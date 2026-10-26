"""Recipient-bound Telegram app provisioning. No application credentials in source."""
import base64,json,os,re,time
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sync_protocol import derive,public_text
from access_lease import canonical

DOMAIN=b'DVK_APP_DELIVERY_V1\n'
PROTOCOL='DVK_APP_REQUEST_V1'

def b64(value):return base64.b64encode(value).decode('ascii')

def validate_request(request):
    if set(request)!={'protocol','request_id','nonce','public_key','manager_id'} or request['protocol']!=PROTOCOL:
        raise ValueError('Invalid app request')
    if not all(isinstance(request[k],str) and re.fullmatch('[a-f0-9]{32}',request[k]) for k in ('request_id','nonce')):
        raise ValueError('Invalid request identity')
    X25519PublicKey.from_public_bytes(base64.b64decode(request['public_key'],validate=True))
    if not isinstance(request['manager_id'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',request['manager_id']):
        raise ValueError('Invalid manager identity')
    return request

def issue(signing_key,request,account_id,profile,now=None):
    validate_request(request);now=int(time.time() if now is None else now)
    if type(account_id) is not int or account_id<=0:raise ValueError('Invalid account')
    payload=dict(protocol='DVK_APP_RESPONSE_V1',request_id=request['request_id'],nonce=request['nonce'],
                 manager_id=request['manager_id'],account_id=account_id,issued_at=now,expires_at=now+900,profile=profile)
    signed=dict(payload=payload,signature=b64(signing_key.sign(DOMAIN+canonical(payload))))
    ephemeral=X25519PrivateKey.generate();nonce=os.urandom(12)
    peer=X25519PublicKey.from_public_bytes(base64.b64decode(request['public_key'],validate=True))
    encrypted=AESGCM(derive(ephemeral.exchange(peer),DOMAIN)).encrypt(nonce,canonical(signed),DOMAIN)
    return dict(protocol='DVK_APP_SEALED_V1',peer=public_text(ephemeral),nonce=b64(nonce),data=b64(encrypted))

def open_response(private,packet,request,center_public,*,expected_account=None,now=None):
    validate_request(request)
    if len(canonical(packet))>16384 or packet.get('protocol')!='DVK_APP_SEALED_V1':raise ValueError('Invalid app response')
    peer=X25519PublicKey.from_public_bytes(base64.b64decode(packet['peer'],validate=True))
    plaintext=AESGCM(derive(private.exchange(peer),DOMAIN)).decrypt(
        base64.b64decode(packet['nonce'],validate=True),base64.b64decode(packet['data'],validate=True),DOMAIN)
    signed=json.loads(plaintext);payload=signed['payload']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(center_public,validate=True)).verify(
        base64.b64decode(signed['signature'],validate=True),DOMAIN+canonical(payload))
    now=int(time.time() if now is None else now)
    if (payload.get('protocol')!='DVK_APP_RESPONSE_V1' or any(payload.get(k)!=request[k] for k in ('request_id','nonce','manager_id'))
            or type(payload.get('account_id')) is not int or payload['account_id']<=0
            or type(payload.get('issued_at')) is not int or type(payload.get('expires_at')) is not int
            or payload['issued_at']>now+60 or not now<payload['expires_at']<=payload['issued_at']+900
            or (expected_account is not None and payload['account_id']!=expected_account)):
        raise ValueError('App response does not match this installation')
    return signed
