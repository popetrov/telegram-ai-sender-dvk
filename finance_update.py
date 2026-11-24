"""Read the center's signed Finance channel assignment at ordinary startup."""

import base64
import contextlib
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3

from access_lease import AccessDenied, LeaseStore
from startup_update import REPOSITORY, fetch, version


CHANNEL = 'dvk-finance'
MARKER = 'data/dvk-finance-update-channel'


def discover(root, active, current, manager):
    """Return only the update assigned to this enrolled database and account."""
    root, active = Path(root), Path(active)
    marker = root/MARKER
    if marker.is_symlink() or marker.read_text('ascii').strip() != CHANNEL:
        raise ValueError('Invalid Finance channel enrollment')
    db_file = root/'data/local.sqlite'
    lease_file = root/'data/access_lease.json'
    if db_file.is_symlink() or lease_file.is_symlink():
        raise ValueError('Invalid Finance account data path')
    try:
        with contextlib.closing(sqlite3.connect(db_file.as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
            identity = dict(db.execute("SELECT key,value FROM local_settings WHERE key IN ('manager_id','account_id')"))
        account = identity.get('account_id', '')
        if identity.get('manager_id') != manager or not re.fullmatch(r'[1-9][0-9]{0,19}', account):
            return None
        profile = json.loads((active/'center-public.json').read_text('utf-8'))
        key = base64.b64decode(profile['access_public_key'], validate=True)
        lease = LeaseStore(lease_file, key, f'{manager}:{account}').require_access()
    except (AccessDenied, sqlite3.Error):
        return None
    grant = lease.get('update_channel')
    if not isinstance(grant, dict) or set(grant) != {'name','version','sha256'} or grant['name'] != CHANNEL:
        return None
    tag, checksum = grant['version'], grant['sha256']
    if (not isinstance(tag, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', tag)
            or not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum)):
        raise ValueError('Invalid signed Finance update')
    if version(tag) <= version(current):
        return None
    url = f'https://github.com/{REPOSITORY}/releases/download/finance-v{tag}/dvk-transition-pilot.zip'
    raw = fetch(url)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), checksum):
        raise ValueError('Finance update checksum mismatch')
    return 'v'+tag, raw
