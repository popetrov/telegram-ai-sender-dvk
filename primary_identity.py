"""Bind the primary database before any new client work; never silently reassign."""
import hashlib,json,os,platform,sqlite3,sys,time,uuid,re
from contextlib import closing
from pathlib import Path
_bound=None

def activate_binding(account,manager,data_store_id):
    global _bound
    _bound=(str(account),manager,data_store_id)

def require_connection(db):
    if _bound is None:return
    values=dict(db.execute("SELECT key,value FROM local_settings WHERE key IN ('account_id','manager_id','data_store_id')"))
    if tuple(values.get(k) for k in ('account_id','manager_id','data_store_id'))!=_bound:
        raise ValueError('Primary database changed identity while running; client work stopped')

def require_current(path):
    if _bound is None:return
    p=Path(path)
    if p.is_symlink():raise ValueError('Unsafe primary database')
    with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:require_connection(db)

def bind(database,monitor,account,manager):
    if type(account) is not int or account<=0 or not manager:raise ValueError('Missing real Telegram identity')
    with closing(monitor.connect()) as db:
        row=db.execute("SELECT value FROM monitor_settings WHERE key='subject'").fetchone()
        previous=row[0] if row else None
    subject=manager+':'+str(account)
    if previous is not None and previous!=subject:raise ValueError('Journal belongs to another Telegram account')
    with closing(sqlite3.connect(database,timeout=30)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'local_settings' not in tables:raise ValueError('Primary database not initialized')
        old=dict(db.execute("SELECT key,value FROM local_settings WHERE key IN ('manager_id','account_id','data_store_id')"))
        if old.get('manager_id')!=manager:raise ValueError('Primary manager mismatch')
        if old.get('account_id') not in (None,str(account)):raise ValueError('Primary Telegram account mismatch')
        populated=any(db.execute('SELECT 1 FROM '+t+' LIMIT 1').fetchone() for t in ['clients','messages'] if t in tables)
        if populated and old.get('account_id') is None and previous!=subject:
            raise ValueError('Existing database needs identity verification; no implicit account adoption')
        data_id=old.get('data_store_id') or uuid.uuid4().hex
        if not re.fullmatch('[0-9a-f]{32}',data_id):raise ValueError('Invalid primary store identity')
        stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        for key,value in [('account_id',str(account)),('data_store_id',data_id)]:
            db.execute('INSERT INTO local_settings(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING',(key,value,stamp))
    return data_id

def snapshot(client,data_store_id):
    import telethon
    from installation_paths import state_root,code_root
    root=state_root();code=code_root();key=getattr(getattr(client.session,'auth_key',None),'key',None)
    evidence=dict(data_store_id=data_store_id,account_id=client._self_id,parent_pid=os.getppid(),
                  python_version=platform.python_version(),sdk_version=telethon.__version__,
                  coverage='queue_and_socket_witness' if getattr(client._sender,'_dvk_forensic_guard',False) else 'transport_gap',
                  schema_version=1,monotonic_ns=time.monotonic_ns())
    pair=str(getattr(client,'api_id',''))+':'+str(getattr(client,'api_hash',''))
    evidence['application_fingerprint']=hashlib.sha256(('DVK-APP:'+pair).encode()).hexdigest()
    if key:evidence['session_fingerprint']=hashlib.sha256(b'DVK-AUTH-KEY:'+key).hexdigest()
    if len(code.name)==32 and (root/'.dvk/layout.json').is_file():evidence['runtime_id']=code.name
    return evidence
