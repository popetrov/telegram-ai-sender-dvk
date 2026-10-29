"""Owner-only company subscriptions, bound Telegram identities and durable receipts."""
import calendar
import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


def renewal_end(current_end, now=None):
    now = time.time() if now is None else now
    base = datetime.fromtimestamp(max(current_end or 0, now), ZoneInfo("Asia/Dubai"))
    year, month = base.year + (base.month == 12), base.month % 12 + 1
    return int((base.replace(year=year, month=month,
                 day=min(base.day, calendar.monthrange(year, month)[1])) + timedelta(days=2)).timestamp())


class AuthorityStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at INTEGER, perpetual INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO companies(id, perpetual) VALUES ('dvk', 1);
                CREATE TABLE IF NOT EXISTS managers (
                    account_id INTEGER PRIMARY KEY, subject TEXT NOT NULL,
                    company_id TEXT REFERENCES companies(id), enabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    account_id INTEGER NOT NULL, event_id TEXT NOT NULL,
                    digest TEXT NOT NULL, payload TEXT NOT NULL, received_at INTEGER NOT NULL,
                    company_id TEXT NOT NULL, subject TEXT NOT NULL,
                    PRIMARY KEY(account_id, event_id));
                CREATE TABLE IF NOT EXISTS fragments (
                    account_id INTEGER NOT NULL, parent TEXT NOT NULL, part INTEGER NOT NULL,
                    count INTEGER NOT NULL, digest TEXT NOT NULL, chunk BLOB NOT NULL,
                    PRIMARY KEY(account_id,parent,part));
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, account_id, subject):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO managers(account_id, subject) VALUES (?,?)', (account_id, subject))

    def company(self, company_id, enabled=True):
        if not company_id or len(company_id) > 64 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in company_id):
            raise ValueError('ID компании: латинские буквы, цифры, _ или -')
        with self.connection() as db:
            db.execute('INSERT INTO companies(id,enabled) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled',
                       (company_id, int(enabled)))

    def renew(self, company_id, now=None):
        with self.connection() as db:
            row = db.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
            if row is None or row['perpetual']:
                raise ValueError('Компания не найдена или уже имеет бессрочный доступ')
            end = renewal_end(row['expires_at'], now)
            db.execute('UPDATE companies SET expires_at=? WHERE id=?', (end, company_id))
            return end

    def assign(self, account_id, company_id):
        with self.connection() as db:
            if db.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone() is None:
                raise ValueError('Сначала создайте компанию')
            previous = db.execute('SELECT company_id FROM managers WHERE account_id=?', (account_id,)).fetchone()
            if previous and previous['company_id'] not in (None, company_id):
                raise ValueError('Менеджер уже связан с другой компанией. Нужна отдельная миграция данных.')
            if db.execute('UPDATE managers SET company_id=?, enabled=1 WHERE account_id=?',
                          (company_id, account_id)).rowcount != 1:
                raise ValueError('Сначала менеджер должен отправить запрос доступа')

    def revoke(self, account_id):
        with self.connection() as db:
            db.execute('UPDATE managers SET enabled=0 WHERE account_id=?', (account_id,))

    def permission(self, account_id, subject, now=None):
        now = int(time.time() if now is None else now)
        with self.connection() as db:
            row = db.execute('''SELECT m.subject, m.enabled AS manager_enabled, c.* FROM managers m
                JOIN companies c ON c.id=m.company_id WHERE account_id=?''', (account_id,)).fetchone()
        allowed = bool(row and row['subject'] == subject and row['manager_enabled'] and row['enabled']
                       and (row['perpetual'] or (row['expires_at'] or 0) > now))
        return allowed, (None if not row or row['perpetual'] else row['expires_at'])

    def list_managers(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM managers ORDER BY account_id')]

    def list_companies(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM companies ORDER BY id')]

    def receive(self, account_id, subject, event_id, digest, payload):
        # Revoked/expired managers may still deliver previously collected data.
        if payload.get('data', {}).get('table') == 'sync_batch':
            return self.receive_batch(account_id, subject, event_id, digest, payload)
        with self.connection() as db:
            manager = db.execute('SELECT * FROM managers WHERE account_id=?', (account_id,)).fetchone()
            if not manager or manager['subject'] != subject or not manager['company_id']:
                raise ValueError('Unbound identity')
            if payload.get('data', {}).get('table') == 'sync_fragment':
                row = payload['data']['row']
                parent, original_digest = row['parent'], row['digest']
                index, count = row['index'], row['count']
                if (not isinstance(parent, str) or len(parent) != 32
                        or any(c not in '0123456789abcdef' for c in parent)
                        or not isinstance(original_digest, str) or len(original_digest) != 64
                        or type(index) is not int or type(count) is not int or not 0 <= index < count <= 256):
                    raise ValueError('Invalid fragments')
                expected_id = hashlib.sha256(f'{parent}:{original_digest}:{index}'.encode()).hexdigest()[:32]
                if expected_id != event_id:
                    raise ValueError('Fragment identity mismatch')
                completed = db.execute('SELECT digest FROM events WHERE account_id=? AND event_id=?',
                                       (account_id, parent)).fetchone()
                if completed:
                    if completed['digest'] != original_digest:
                        raise ValueError('Conflicting completed event')
                    return
                chunk = base64.b64decode(row['chunk'], validate=True)
                if not chunk or len(chunk) > 256 * 1024:
                    raise ValueError('Invalid chunk size')
                old = db.execute('SELECT * FROM fragments WHERE account_id=? AND parent=?', (account_id,parent)).fetchall()
                for part in old:
                    if part['count'] != count or part['digest'] != original_digest:
                        raise ValueError('Fragment metadata conflict')
                    if part['part'] == index and part['chunk'] != chunk:
                        raise ValueError('Fragment content conflict')
                db.execute('INSERT OR IGNORE INTO fragments VALUES (?,?,?,?,?,?)',
                           (account_id,parent,index,count,original_digest,chunk))
                parts = db.execute('SELECT * FROM fragments WHERE account_id=? AND parent=? ORDER BY part',
                                   (account_id,parent)).fetchall()
                if len(parts) < count:
                    return
                raw = b''.join(part['chunk'] for part in parts)
                if hashlib.sha256(raw).hexdigest() != original_digest:
                    raise ValueError('Assembled digest mismatch')
                payload = json.loads(raw)
                from sync_data import TABLES
                if payload.get('data', {}).get('table') not in TABLES:
                    raise ValueError('Invalid assembled event')
                event_id, digest = parent, original_digest
                db.execute('DELETE FROM fragments WHERE account_id=? AND parent=?', (account_id,parent))
            old = db.execute('SELECT digest FROM events WHERE account_id=? AND event_id=?',
                             (account_id, event_id)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('Conflicting event ID')
            if payload.get('data',{}).get('table')=='monitor_events':
                from incident_monitor import validate_bound_event
                validate_bound_event(payload,account_id,subject)
            db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)',
                       (account_id, event_id, digest, json.dumps(payload, ensure_ascii=False),
                        int(time.time()), manager['company_id'], subject))

    def receive_batch(self, account_id, subject, event_id, digest, payload):
        from sync_protocol import batch_members, event_digest
        members = batch_members(payload)
        if digest != event_digest(payload) or event_id != digest[:32]:
            raise ValueError('Sync batch identity mismatch')
        with self.connection() as db:
            manager = db.execute('SELECT * FROM managers WHERE account_id=?', (account_id,)).fetchone()
            if not manager or manager['subject'] != subject or not manager['company_id']:
                raise ValueError('Unbound identity')
            for member_id, member in members:
                if member.get('data',{}).get('table')=='monitor_events':
                    from incident_monitor import validate_bound_event
                    validate_bound_event(member,account_id,subject)
                member_digest = event_digest(member)
                old = db.execute('SELECT digest FROM events WHERE account_id=? AND event_id=?',
                                 (account_id, member_id)).fetchone()
                if old and old['digest'] != member_digest:
                    raise ValueError('Conflicting batch member ID')
                db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)',
                           (account_id, member_id, member_digest, json.dumps(member, ensure_ascii=False),
                            int(time.time()), manager['company_id'], subject))
            # The context commits every member together before the caller signs
            # the receipt. A conflict rolls back earlier members of this batch.
