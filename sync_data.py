"""SQLite transactional outbox. Only explicit business tables are exported."""
import json
import uuid

TABLES = ('clients', 'research_runs', 'messages', 'imports', 'prepared_messages', 'uploaded_rows')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS sync_outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
               'event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS uploaded_rows (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    # Stable random identity lives in the same database and survives restarts.
    db.execute("INSERT OR IGNORE INTO local_settings VALUES ('sync_installation_id', ?, datetime('now'))",
               (str(uuid.uuid4()),))
    for table in TABLES:
        columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})')]
        fields = ','.join("'%s',NEW.\"%s\"" % (c, c) for c in columns)
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS sync_{table}_{operation.lower()}
                AFTER {operation} ON {table} BEGIN
                INSERT INTO sync_outbox(event_id,payload) VALUES(lower(hex(randomblob(16))),
                    json_object('table','{table}','row',json_object({fields})));
                END''')
    marker = db.execute("SELECT 1 FROM local_settings WHERE key='sync_initial_export'").fetchone()
    if marker is None:
        for table in TABLES:
            for row in db.execute(f'SELECT * FROM {table}').fetchall():
                enqueue(db, {'table': table, 'row': dict(row)})
        db.execute("INSERT INTO local_settings VALUES ('sync_initial_export','1',datetime('now'))")


def enqueue(db, payload):
    db.execute('INSERT INTO sync_outbox(event_id,payload) VALUES (?,?)',
               (uuid.uuid4().hex, json.dumps(payload, ensure_ascii=False)))


def next_event(db):
    row = db.execute('SELECT * FROM sync_outbox ORDER BY sequence LIMIT 1').fetchone()
    if row is None:
        return None
    installation = db.execute("SELECT value FROM local_settings WHERE key='sync_installation_id'").fetchone()[0]
    return row['event_id'], dict(installation=installation, data=json.loads(row['payload']))


def next_transfer(db):
    """Pack consecutive pending events; oversized single events keep fragmentation."""
    from sync_protocol import MAX_BATCH_RECORDS, MAX_BATCH_BYTES, event_digest
    from access_lease import canonical
    installation = db.execute("SELECT value FROM local_settings WHERE key='sync_installation_id'").fetchone()[0]
    events = []
    envelope = dict(installation=installation, data=dict(table='sync_batch', row=dict(events=events)))
    for row in db.execute('SELECT event_id,payload FROM sync_outbox ORDER BY sequence LIMIT ?', (MAX_BATCH_RECORDS,)):
        event = dict(event_id=row['event_id'], data=json.loads(row['payload']))
        events.append(event)
        if len(canonical(envelope)) > MAX_BATCH_BYTES:
            events.pop()
            if not events:
                single = dict(installation=installation, data=event['data'])
                return [event['event_id']], event['event_id'], single
            break
    if not events:
        return None
    if len(events) == 1:
        event = events[0]
        return [event['event_id']], event['event_id'], dict(installation=installation, data=event['data'])
    return [event['event_id'] for event in events], event_digest(envelope)[:32], envelope


def acknowledge_transfer(db, event_ids):
    """Caller holds the transaction and has verified the whole signed receipt."""
    db.executemany('DELETE FROM sync_outbox WHERE event_id=?', ((event_id,) for event_id in event_ids))
