"""Persistent isolation of one affected private chat; no Telegram mutations."""
import json,sqlite3,time
from contextlib import closing
from pathlib import Path

class PeerQuarantined(PermissionError):
    """The transport refused dispatch before any client message was sent."""

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS monitor_peer_blocks (
      peer_id INTEGER PRIMARY KEY,created_at INTEGER NOT NULL,reason TEXT NOT NULL,
      notified INTEGER NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS monitor_deleted_messages (
      message_id INTEGER PRIMARY KEY,peer_id INTEGER NOT NULL,created_at INTEGER NOT NULL,
      source TEXT NOT NULL)''')

def require_peer_allowed(peer_id,recorder=None):
    if type(peer_id) is not int or peer_id<=0:return
    if recorder is None:
        from incident_monitor import _default
        recorder=_default
    p=Path(recorder.path)
    if not p.exists():return
    if p.is_symlink():raise RuntimeError('Небезопасный журнал изоляции диалогов')
    with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_peer_blocks'").fetchone():return
        if db.execute('SELECT 1 FROM monitor_peer_blocks WHERE peer_id=?',(peer_id,)).fetchone():
            raise PeerQuarantined('В этом диалоге исчезло сообщение. Автоматические исходящие ему остановлены; другие диалоги не затронуты.')

def _resolve(recorder,ids,database):
    peers={};unknown=[]
    with closing(recorder.connect()) as db:
        bound=db.execute("SELECT value FROM monitor_settings WHERE key='subject'").fetchone()
        subject=bound[0] if bound else None
        for mid in ids:
            row=db.execute('SELECT peer_id FROM monitor_messages WHERE message_id=?',(mid,)).fetchone()
            if row and type(row[0]) is int and row[0]>0:peers[mid]=row[0]
            else:unknown.append(mid)
    if not unknown:return peers,[]
    if not subject or not Path(database).exists():return peers,unknown
    manager,account=subject.rsplit(':',1)
    if not account.isdigit():raise ValueError('Unbound monitor account')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for mid in list(unknown):
            found=set()
            if {'messages','clients'}<=tables:
                found.update(r[0] for r in db.execute('SELECT c.telegram_id FROM messages m JOIN clients c ON c.id=m.client_id WHERE m.telegram_message_id=? AND c.manager_id=? AND c.telegram_id>0',(mid,manager)))
            if {'dialogue_threads','dialogue_messages'}<=tables:
                found.update(r[0] for r in db.execute('SELECT t.peer_id FROM dialogue_messages m JOIN dialogue_threads t ON t.id=m.thread_id WHERE m.telegram_id=? AND t.account_id=? AND t.manager_id=? AND t.peer_id>0',(mid,int(account),manager)))
            if len(found)==1:peers[mid]=found.pop();unknown.remove(mid)
    return peers,unknown

def isolate(recorder,ids,database,source):
    ids=list(dict.fromkeys(ids));peers,unknown=_resolve(recorder,ids,database)
    now=int(time.time());new=set()
    with closing(recorder.connect()) as db,db:
        initialize(db)
        for mid,peer in peers.items():
            old=db.execute('SELECT peer_id FROM monitor_deleted_messages WHERE message_id=?',(mid,)).fetchone()
            if old and old[0]!=peer:raise ValueError('Conflicting deleted-message binding')
            db.execute('INSERT OR IGNORE INTO monitor_deleted_messages VALUES(?,?,?,?)',(mid,peer,now,source))
            if db.execute('INSERT OR IGNORE INTO monitor_peer_blocks(peer_id,created_at,reason) VALUES(?,?,?)',(peer,now,'observed_message_missing')).rowcount:new.add(peer)
    # Existing dialogue states, pending replies and calendar hooks stop only this peer.
    if peers and Path(database).exists():
        from dialogue_store import DialogueStore
        store=DialogueStore(database)
        with store.connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='dialogue_threads'").fetchone():
                bound=None
                with closing(recorder.connect()) as meta:bound=meta.execute("SELECT value FROM monitor_settings WHERE key='subject'").fetchone()
                if bound:
                    manager,account=bound[0].rsplit(':',1)
                    targets=[dict(r) for r in db.execute('SELECT * FROM dialogue_threads WHERE manager_id=? AND account_id=?',(manager,int(account))) if r['peer_id'] in set(peers.values()) and r['state'] not in {'closed','opt_out'}]
                else:targets=[]
            else:targets=[]
        for t in targets:store.set_state(t['id'],'closed',expected_revision=t['revision'])
    return dict(peers=sorted(set(peers.values())),new_peers=sorted(new),unresolved=unknown)

async def notify_pending(recorder,notify):
    with closing(recorder.connect()) as db:
        initialize(db)
        pending=[r[0] for r in db.execute('SELECT peer_id FROM monitor_peer_blocks WHERE notified=0 ORDER BY created_at LIMIT 10')]
    for peer in pending:
        try:
            await notify('В диалоге '+str(peer)+' исчезло сообщение. Этот диалог исключён из автоматических отправок; другие диалоги этим событием не блокируются. Кто удалил сообщение, Telegram не сообщает. Запись сохранена для проверки.')
        except Exception:continue
        with closing(recorder.connect()) as db,db:db.execute('UPDATE monitor_peer_blocks SET notified=1 WHERE peer_id=?',(peer,))


def migrate_observed_pause(recorder,database,journal):
    """Upgrade an observed deletion into durable peer blocks, retaining all evidence.
    Forbidden calls, unresolved identities and damaged journals never auto-resume.
    Called synchronously at startup before business workers start.
    """
    import hashlib,os
    old=journal.current()
    if not old or old['operation'] not in {'observed_telegram_deletion','observed_history_missing'}:return None
    if journal._volatile is not None:return None
    ids=set()
    with closing(recorder.connect()) as db:
        for row in db.execute('SELECT data FROM monitor_events'):
            e=json.loads(row[0])
            if e['recorded_at']<old['created_at']:continue
            if e['kind']=='request_blocked':return None
            if e['kind']=='history_missing' or (e['kind']=='telegram_deleted' and e.get('source')=='tracked_private_history'):
                ids.update(e.get('message_ids',[]))
    if not ids:return None
    scope=isolate(recorder,sorted(ids),database,'approved_scope_migration')
    if scope['unresolved']:return None
    current=journal.current()
    if current is None or current['incident_id']!=old['incident_id']:return None
    path=journal.path;archive=path.parent/'safety-archive';archive.mkdir(exist_ok=True)
    if path.is_symlink() or archive.is_symlink():return None
    target=archive/(old['incident_id']+'.sqlite')
    if target.exists() or Path(str(path)+'-wal').exists() or Path(str(path)+'-journal').exists():return None
    resolution=dict(incident=old,scope=scope,at=int(time.time()),policy='peer_isolation_upgrade',sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    # Persist the explanation first. Failure leaves the original global stop active.
    record=target.with_suffix('.resolution.json');record.write_text(json.dumps(resolution,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(path,target)
    return resolution


def reconcile_history(recorder,database):
    """Keep already-observed missing messages isolated across upgrades and restarts."""
    ids=set()
    with closing(recorder.connect()) as db:
        for row in db.execute('SELECT data FROM monitor_events'):
            e=json.loads(row[0])
            if e['kind']=='history_missing' or (e['kind']=='telegram_deleted' and e.get('source')=='tracked_private_history'):
                ids.update(e.get('message_ids',[]))
    return isolate(recorder,sorted(ids),database,'history_reconciliation') if ids else dict(peers=[],new_peers=[],unresolved=[])
