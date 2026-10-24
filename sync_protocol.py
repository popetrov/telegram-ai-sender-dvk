"""End-to-end encrypted upload and signed, content-bound durable receipt."""
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from access_lease import canonical

MAX_PACKET = 2 * 1024 * 1024
AAD = b'DVK_SYNC_V1'
CHUNK_BYTES = 256 * 1024
MAX_EVENT = 64 * 1024 * 1024
MAX_BATCH_RECORDS = 100
MAX_BATCH_BYTES = 512 * 1024


def b64(data):
    return base64.b64encode(data).decode('ascii')


def initialize_encryption_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return X25519PrivateKey.from_private_bytes(path.read_bytes())
    key = X25519PrivateKey.generate()
    with path.open('xb') as stream:
        stream.write(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    if os.name != 'nt':
        path.chmod(0o600)
    return key


def public_text(key):
    return b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def derive(shared):
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=AAD).derive(shared)


def encrypt(public, payload):
    content = canonical(payload)
    if len(content) > MAX_PACKET // 2:
        raise ValueError('Sync event exceeds size limit')
    ephemeral = X25519PrivateKey.generate()
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
    nonce = os.urandom(12)
    encrypted = AESGCM(derive(ephemeral.exchange(peer))).encrypt(nonce, content, AAD)
    return canonical(dict(version=1, peer=public_text(ephemeral), nonce=b64(nonce), data=b64(encrypted)))


def decrypt(key, packet):
    if len(packet) > MAX_PACKET:
        raise ValueError('Sync packet exceeds limit')
    wrapper = json.loads(packet)
    if wrapper['version'] != 1:
        raise ValueError('Unsupported sync packet')
    peer = X25519PublicKey.from_public_bytes(base64.b64decode(wrapper['peer'], validate=True))
    content = AESGCM(derive(key.exchange(peer))).decrypt(
        base64.b64decode(wrapper['nonce'], validate=True),
        base64.b64decode(wrapper['data'], validate=True), AAD)
    return json.loads(content)


def receipt(key, subject, event_id, digest):
    payload = dict(protocol='DVK_RECEIPT_V1', subject=subject, event_id=event_id, digest=digest)
    return dict(payload=payload, signature=b64(key.sign(canonical(payload))))


def verify_receipt(key, envelope, subject, event_id, digest):
    payload = envelope['payload']
    key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(payload))
    if payload != dict(protocol='DVK_RECEIPT_V1', subject=subject, event_id=event_id, digest=digest):
        raise ValueError('Receipt belongs to another event')


def event_digest(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def batch_members(payload):
    """Validate the whole bounded batch before opening a write transaction."""
    from sync_data import TABLES
    if (not isinstance(payload, dict) or not isinstance(payload.get('installation'), str)
            or not payload['installation'] or len(canonical(payload)) > MAX_BATCH_BYTES):
        raise ValueError('Invalid sync batch envelope')
    data = payload.get('data', {})
    if not isinstance(data, dict) or data.get('table') != 'sync_batch':
        raise ValueError('Invalid sync batch type')
    row = data.get('row', {})
    events = row.get('events') if isinstance(row, dict) else None
    if not isinstance(events, list) or not 2 <= len(events) <= MAX_BATCH_RECORDS:
        raise ValueError('Invalid sync batch length')
    result, seen = [], set()
    for event in events:
        if not isinstance(event, dict) or set(event) != {'event_id', 'data'}:
            raise ValueError('Invalid sync batch member')
        event_id, business = event['event_id'], event['data']
        if (not isinstance(event_id, str) or len(event_id) != 32
                or any(c not in '0123456789abcdef' for c in event_id) or event_id in seen
                or not isinstance(business, dict) or business.get('table') not in TABLES
                or not isinstance(business.get('row'), dict)):
            raise ValueError('Invalid or duplicate sync batch event')
        seen.add(event_id)
        result.append((event_id, dict(installation=payload['installation'], data=business)))
    return result


def event_parts(event_id, data):
    """Stable wire IDs allow retries even if the last multipart receipt was lost."""
    raw = canonical(data)
    if len(raw) <= MAX_PACKET // 2:
        return [(event_id, data)]
    if len(raw) > MAX_EVENT:
        raise ValueError('Business event exceeds 64 MiB; retained locally')
    digest = hashlib.sha256(raw).hexdigest()
    count = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    result = []
    for index in range(count):
        wire_id = hashlib.sha256(f'{event_id}:{digest}:{index}'.encode()).hexdigest()[:32]
        row = dict(parent=event_id, digest=digest, index=index, count=count,
                   chunk=b64(raw[index*CHUNK_BYTES:(index+1)*CHUNK_BYTES]))
        result.append((wire_id, dict(installation=data['installation'], data=dict(table='sync_fragment', row=row))))
    return result
