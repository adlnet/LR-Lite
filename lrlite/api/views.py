from pyramid.httpexceptions import HTTPBadRequest, HTTPNotFound
from LRSignature.sign.Sign import Sign_0_21, Sign_0_23, Sign_0_49, Sign_0_51
import gnupg
from pyramid.view import view_config
from pyramid.response import Response
from lib.validation import *
from logging import getLogger
import base64
import json
import uuid
import calendar
from datetime import datetime
import requests
import ijson
import iso8601
import pdb
log = getLogger(__name__)
gpg_location = "/usr/bin/gpg"
_TOMBSTONE = "tombstone"
_DOC_TYPE = 'doc_type'
_USER_PREFIX = "org.couchdb.user:"
_DOC_ID = "doc_ID"
_NODE_TIMESTAMP = "node_timestamp"
_CREATE_TIMESTAMP = "create_timestamp"
_UPDATE_TIMESTAMP = "update_timestamp"
_PUBLISHING_NODE = 'publishing_node'
_DIGITAL_SIG = 'digital_signature'
_START_KEY = "startkey"
_END_KEY = 'endkey'
_INCLUDE_DOCS = 'include_docs'
_FROM = "from"
_UNTIL = "until"
_PAGE = "page"
_PAGE_SIZE = 25

def _get_signer_for_version(version, private_key):    
    if version == "0.21.0":
        return Sign_0_21(privateKeyID=private_key, gpgbin=gpg_location)
    elif version == "0.23.0":
        return Sign_0_23(privateKeyID=private_key, gpgbin=gpg_location)
    elif version == "0.49.0":
        return Sign_0_49(privateKeyID=private_key, gpgbin=gpg_location)        
    elif version == "0.51.0":
        return Sign_0_51(privateKeyID=private_key, gpgbin=gpg_location)


def _populate_node_values(envelope, req):
    if _DOC_ID not in envelope:
        doc_id = uuid.uuid4().hex
        envelope[_DOC_ID] = doc_id
    current_time = datetime.utcnow().isoformat() + 'Z'
    for k in [_NODE_TIMESTAMP, _CREATE_TIMESTAMP, _UPDATE_TIMESTAMP]:
        envelope[k] = current_time
    envelope[_PUBLISHING_NODE] = req.node_id


def _parse_retrieve_params(req):
    params = {"limit": _PAGE_SIZE, "stale": "update_after"}
    include_docs = req.GET.get(_INCLUDE_DOCS, "false")
    try:
        include_docs = json.loads(include_docs)
    except Exception as ex:
        raise HTTPBadRequest("Invalid JSON for include_docs")
    params[_INCLUDE_DOCS] = include_docs
    try:
        time = iso8601.parse_date(req.GET.get(_FROM, datetime.min.isoformat()))
        params[_START_KEY] = calendar.timegm(time.utctimetuple())
    except Exception as ex:
        raise HTTPBadRequest("Invalid from time, must be ISO 8601 format")
    try:
        time = iso8601.parse_date(
            req.GET.get(_UNTIL, datetime.utcnow().isoformat()))
        params[_END_KEY] = calendar.timegm(time.utctimetuple())
    except Exception as ex:
        raise HTTPBadRequest("Invalid until time, must be ISO 8601 format")
    if params[_END_KEY] < params[_START_KEY]:
        raise HTTPBadRequest("From date cannot come after until date")
    if _PAGE in req.GET:
        try:
            page = int(req.GET.get(_PAGE))
            params['skip'] = page * _PAGE_SIZE
        except:
            raise HTTPBadRequest("Page must be a valid integer")
    return params


def _validate_document(data, req):
    if data[_DOC_ID] in req.db:
        raise Exception("doc_ID is taken")
    result = validate_schema(data)
    if not result.success:
        raise Exception(result.message)
    if "digital_signature" not in data:
        user_info = req.users[_USER_PREFIX + req.username]
        signer = _get_signer_for_version(data.get("doc_version"), user_info.get("keyid"))        
        signer.sign(data)
        data["digital_signature"]['key_location'] = [req.route_url("userkey", username=req.username)]
    else:    
        result = validate_signature(data)
        if result.success == False:
            raise Exception(result.message)

def _validate_signature(signer, req, doc_id):
    gpg = gnupg.GPG()
    user_info = req.users["org.couchdb.user:" + req.username]
    keys = [k for k in gpg.list_keys() if k['keyid'] == user_info['keyid']]    
    if len(keys) <= 0:
        raise HTTPBadRequest("Cannot delete document on this server")
    key = keys.pop()
    doc_owner = False    
    for uid in key.get('uids', []):
        if uid == signer:
            doc_owner = True
    return doc_owner

@view_config(route_name="api", renderer="json", request_method="PUT", permission="user")
def add_envelope(req):
    try:
        data = json.loads(req.body)
    except:
        raise HTTPBadRequest("Body must contain valid json")
    _populate_node_values(data, req)
    data['_id'] = data[_DOC_ID]    
    try:
        _validate_document(data, req)
    except Exception as ex:
        return {"OK": False, "msg": ex.message}
    resp = requests.post(req.db.uri, data=json.dumps(data),
                         headers={
                             "Content-Type": "application/json",
                             'set-cookie': req.auth_cookie}
                         )
    if resp.ok:
        return {"OK": True, _DOC_ID: data[_DOC_ID]}
    else:
        raise Exception("Couchdb not available")


@view_config(route_name="document", renderer="json", request_method="POST", permission="user")
def update_document(req):
    doc_id = req.matchdict['doc_id']    
    if doc_id not in req.db:
        raise HTTPBadRequest("Document does not exist")    
    try:
        data = json.loads(req.body)
    except:
        raise HTTPBadRequest("Body must contain valid json")
    _populate_node_values(data, req)
    old_doc = req.db[doc_id]
    doc_owner = _validate_signature(old_doc.get("digital_signature", {}).get("key_owner"), req, doc_id)
    if not doc_owner:
        raise HTTPBadRequest("Cannot delete document on this server")
    old_doc[_DOC_TYPE] = _TOMBSTONE
    try:
        _validate_document(data, req)
    except Exception as ex:
        return {"OK": False, "msg": ex.message}
    resp = requests.post(req.db.uri, data=json.dumps(old_doc),
                         headers={
                             "Content-Type": "application/json",
                             'set-cookie': req.auth_cookie}
                         )
    data['_id'] = data[_DOC_ID]
    resp = requests.post(req.db.uri, data=json.dumps(data),
                         headers={
                             "Content-Type": "application/json",
                             'set-cookie': req.auth_cookie}
                         )    
    if resp.ok:
        return {"OK": True, _DOC_ID: data[_DOC_ID]}
    else:
        raise Exception("Couchdb not available")


def _get_db_uri(db, params):
    list_function = "ids"
    if params['include_docs']:
        list_function = "docs"
    return db.uri + "/_design/lr/_list/" + list_function + "/by-timestamp"


@view_config(route_name="api", renderer="json", request_method="GET")
def retrieve_list(req):
    params = _parse_retrieve_params(req)
    headers = {
        'Content-Type': 'application/json',
    }
    resp = requests.get(_get_db_uri(req.db, params),
                        stream=True, params=params)
    r = Response(headers=headers)
    r.body_file = resp.raw
    return r


@view_config(route_name="document", renderer="json", request_method="GET")
def retrive_envelope(req):
    try:
        return req.db[req.matchdict.get("doc_id")]
    except:
        raise HTTPNotFound()

@view_config(route_name="document", renderer="json", request_method="DELETE", permission="user")
def delete_document(req):
    doc_id = req.matchdict['doc_id']    
    if doc_id not in req.db:
        raise HTTPBadRequest("Document does not exist")
    gpg = gnupg.GPG()
    user_info = req.users["org.couchdb.user:" + req.username]
    keys = [k for k in gpg.list_keys() if k['keyid'] == user_info['keyid']]    
    if len(keys) <= 0:
        raise HTTPBadRequest("Cannot delete document on this server")
    key = keys.pop()
    doc_owner = False
    old_doc = req.db[doc_id]
    for uid in key.get('uids', []):
        if uid == old_doc.get("digital_signature", {}).get("key_owner"):
            doc_owner = True
    if not doc_owner:
        raise HTTPBadRequest("Cannot delete document on this server")
    old_doc[_DOC_TYPE] = _TOMBSTONE
    req.db.save_doc(old_doc)
    return {"OK": True}