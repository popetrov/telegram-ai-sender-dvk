"""Durable dialogue state. No network, automatic enrollment, AI or sending.

Kept separate from first-contact message/research records. Runtime enrollment
must verify the account, business and an actual sent first-contact message.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid

STATES = {'waiting', 'engaged', 'later', 'human', 'opt_out', 'closed'}
ACTION_KINDS = {'reply', 'reminder', 'opt_out_ack'}


class StaleDialogue(ValueError):
    pass


class ContactOptOutError(PermissionError):
    pass


def require_contact_allowed(path, *, manager_id, peer_id):
    """First-contact delivery must also respect an opt-out recorded by part two."""
    path=Path(path)
    if not path.exists():return
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    try:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dialogue_threads'").fetchone()
        stopped=db.execute("SELECT state FROM dialogue_threads WHERE manager_id=? AND peer_id=? AND state IN ('opt_out','closed')",
                           (manager_id,peer_id)).fetchone() if exists else None
        if stopped:
            raise ContactOptOutError('Клиент попросил больше не писать. Отправка заблокирована.' if stopped[0]=='opt_out'
                                    else 'Диалог закрыт, в том числе при удалении переписки. Отправка заблокирована.')
    finally:
        db.close()


class DialogueStore:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, *, sync=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS dialogue_threads (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL, business_id TEXT NOT NULL,
                    manager_id TEXT NOT NULL, account_id INTEGER NOT NULL, peer_id INTEGER NOT NULL,
                    product TEXT NOT NULL, first_message_id INTEGER NOT NULL,
                    first_sent_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
                    revision INTEGER NOT NULL DEFAULT 0, history_complete INTEGER NOT NULL DEFAULT 0,
                    next_contact_at INTEGER, updated_at INTEGER NOT NULL,
                    UNIQUE(account_id,peer_id));
                CREATE TABLE IF NOT EXISTS dialogue_messages (
                    thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),
                    telegram_id INTEGER NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL,
                    sent_at INTEGER NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(thread_id,telegram_id));
                CREATE TABLE IF NOT EXISTS dialogue_actions (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),
                    action_key TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    revision INTEGER NOT NULL, due_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', telegram_id INTEGER,
                    created_at INTEGER NOT NULL, UNIQUE(thread_id,action_key));
                CREATE TABLE IF NOT EXISTS dialogue_notices (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),
                    event_key TEXT NOT NULL, event_type TEXT NOT NULL, summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL,
                    UNIQUE(thread_id,event_key));
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(dialogue_actions)')}
            if 'sent_at' not in columns:
                db.execute('ALTER TABLE dialogue_actions ADD COLUMN sent_at INTEGER')
            if sync:
                from sync_data import initialize,enqueue,DIALOGUE_TABLES
                initialize(db)
                if not db.execute("SELECT 1 FROM local_settings WHERE key='sync_dialogues_initialized'").fetchone():
                    for table in DIALOGUE_TABLES:
                        for row in db.execute('SELECT * FROM '+table).fetchall():
                            enqueue(db,dict(table=table,row=dict(row)))
                    db.execute("INSERT INTO local_settings VALUES ('sync_dialogues_initialized','1',datetime('now'))")

    def enroll(self, *, company_id, business_id, manager_id, account_id, peer_id,
               product, first_message_id, first_sent_at):
        for value in (company_id, business_id, manager_id, product):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Missing trusted dialogue identity')
        for value in (account_id, peer_id, first_message_id, first_sent_at):
            if type(value) is not int or value <= 0:
                raise ValueError('Dialogue requires verified positive Telegram identity and timestamp')
        binding = (company_id,business_id,manager_id,account_id,peer_id,product,first_message_id,first_sent_at)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM dialogue_threads WHERE account_id=? AND peer_id=?',
                             (account_id,peer_id)).fetchone()
            if row:
                fields = ('company_id','business_id','manager_id','account_id','peer_id','product','first_message_id','first_sent_at')
                if tuple(row[k] for k in fields) != binding:
                    raise ValueError('Existing dialogue binding must not change implicitly')
                return row['id']
            thread_id = uuid.uuid4().hex
            db.execute('''INSERT INTO dialogue_threads
                (id,company_id,business_id,manager_id,account_id,peer_id,product,first_message_id,first_sent_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', (thread_id,*binding,int(self.clock())))
            return thread_id

    @staticmethod
    def _thread(db, thread_id):
        row = db.execute('SELECT * FROM dialogue_threads WHERE id=?',(thread_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown dialogue')
        return row

    def record_message(self, thread_id, *, telegram_id, direction, text, sent_at, source):
        if (type(telegram_id) is not int or telegram_id <= 0 or type(sent_at) is not int
                or direction not in {'incoming','outgoing'} or source not in {'history','manager','automation','client'}
                or not isinstance(text,str)):
            raise ValueError('Invalid dialogue message')
        if (source == 'client' and direction != 'incoming') or (source in {'manager','automation'} and direction != 'outgoing'):
            raise ValueError('Invalid message direction/source')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread = self._thread(db,thread_id)
            previous = db.execute('SELECT * FROM dialogue_messages WHERE thread_id=? AND telegram_id=?',
                                  (thread_id,telegram_id)).fetchone()
            if previous:
                if previous['direction'] != direction or previous['sent_at'] != sent_at:
                    raise ValueError('Telegram message identity conflict')
                if previous['text'] == text:
                    return False  # Catch-up and live delivery may contain the same message.
                db.execute('UPDATE dialogue_messages SET text=? WHERE thread_id=? AND telegram_id=?',
                           (text,thread_id,telegram_id))
            else:
                db.execute('INSERT INTO dialogue_messages VALUES (?,?,?,?,?,?)',
                           (thread_id,telegram_id,direction,text,sent_at,source))
            state = thread['state']
            if source == 'manager' and state not in {'opt_out','closed'}:
                state = 'human'
                db.execute("UPDATE dialogue_notices SET status='handled' WHERE thread_id=? AND status IN ('pending','delivered')",(thread_id,))
            db.execute('UPDATE dialogue_threads SET revision=revision+1,state=?,updated_at=? WHERE id=?',
                       (state,int(self.clock()),thread_id))
            db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread_id,))
            return True

    def mark_history_complete(self, thread_id):
        with self.connection() as db:
            thread = self._thread(db,thread_id)
            original = db.execute('SELECT direction FROM dialogue_messages WHERE thread_id=? AND telegram_id=?',
                                  (thread_id,thread['first_message_id'])).fetchone()
            if original is None or original['direction'] != 'outgoing':
                raise ValueError('Verified initial outbound message missing from history')
            db.execute('UPDATE dialogue_threads SET history_complete=1 WHERE id=?',(thread_id,))

    def snapshot(self, thread_id):
        with self.connection() as db:
            thread = dict(self._thread(db,thread_id))
            history = [dict(r) for r in db.execute('SELECT * FROM dialogue_messages WHERE thread_id=? ORDER BY sent_at,telegram_id',(thread_id,))]
            return thread,history

    def thread(self,thread_id):
        with self.connection() as db:
            return dict(self._thread(db,thread_id))

    def last_message_id(self,thread_id):
        with self.connection() as db:
            return db.execute('SELECT COALESCE(MAX(telegram_id),0) FROM dialogue_messages WHERE thread_id=?',(thread_id,)).fetchone()[0]

    def set_state(self, thread_id, state, *, expected_revision, next_contact_at=None):
        if state not in STATES or (next_contact_at is not None and (type(next_contact_at) is not int or next_contact_at <= 0)):
            raise ValueError('Invalid dialogue state/deadline')
        if state in {'human','opt_out','closed'} and next_contact_at is not None:
            raise ValueError('Stopped conversations cannot schedule contact')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread = self._thread(db,thread_id)
            if thread['revision'] != expected_revision:
                raise StaleDialogue('History changed during decision')
            if thread['state'] == 'opt_out' and state != 'opt_out':
                raise ValueError('Opt-out requires separate explicit consent before reactivation')
            db.execute('UPDATE dialogue_threads SET state=?,next_contact_at=?,revision=revision+1,updated_at=? WHERE id=?',
                       (state,next_contact_at,int(self.clock()),thread_id))
            db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread_id,))

    def queue_action(self, thread_id, *, action_key, kind, text, expected_revision, due_at):
        if (kind not in ACTION_KINDS or not isinstance(action_key,str) or not action_key
                or not isinstance(text,str) or not text.strip() or len(text)>4096
                or type(due_at) is not int or due_at<=0):
            raise ValueError('Invalid dialogue action')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread = self._thread(db,thread_id)
            if thread['revision'] != expected_revision or not thread['history_complete']:
                raise StaleDialogue('History incomplete or changed during generation')
            allowed = thread['state'] in {'waiting','engaged','later'}
            if kind == 'opt_out_ack':
                allowed = thread['state']=='opt_out'
                if db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND kind='opt_out_ack'",(thread_id,)).fetchone():
                    raise ValueError('Opt-out acknowledgement already recorded')
            if not allowed:
                raise ValueError('Dialogue is paused or closed')
            if db.execute('SELECT 1 FROM dialogue_actions WHERE thread_id=? AND action_key=?',(thread_id,action_key)).fetchone():
                raise ValueError('Action already exists; automatic replay forbidden')
            action_id=uuid.uuid4().hex
            db.execute('''INSERT INTO dialogue_actions
                (id,thread_id,action_key,kind,text,revision,due_at,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                (action_id,thread_id,action_key,kind,text,expected_revision,due_at,int(self.clock())))
            return action_id

    def claim_action(self, action_id):
        """Reserve before network I/O. 'sending' survives crashes and is never retried here."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            action=db.execute('SELECT * FROM dialogue_actions WHERE id=?',(action_id,)).fetchone()
            if action is None or action['status']!='pending' or action['due_at']>self.clock():
                raise ValueError('Action unavailable or not due')
            thread=self._thread(db,action['thread_id'])
            allowed = (thread['state']=='opt_out' if action['kind']=='opt_out_ack'
                       else thread['state'] in {'waiting','engaged','later'})
            if not allowed or not thread['history_complete'] or thread['revision']!=action['revision']:
                raise StaleDialogue('Decision no longer matches current history')
            db.execute("UPDATE dialogue_actions SET status='sending' WHERE id=?",(action_id,))
            return dict(action)

    def finish_action(self, action_id, *, status, telegram_id=None):
        if status not in {'sent','uncertain','not_sent'}:
            raise ValueError('Invalid delivery result')
        if status=='sent' and (type(telegram_id) is not int or telegram_id<=0):
            raise ValueError('Sent result requires a real Telegram message ID')
        with self.connection() as db:
            changed=db.execute("UPDATE dialogue_actions SET status=?,telegram_id=?,sent_at=? WHERE id=? AND status='sending'",
                               (status,telegram_id,int(self.clock()) if status=='sent' else None,action_id)).rowcount
            if changed!=1:
                raise ValueError('Action was not reserved or already completed')

    def handoff(self, thread_id, *, event_key, event_type, summary, pause, expected_revision):
        """Atomically save a manager notice and, if required, pause the dialogue."""
        if (not isinstance(event_key,str) or not event_key or not isinstance(event_type,str) or not event_type
                or not isinstance(summary,str) or not summary.strip() or len(summary)>3000 or type(pause) is not bool):
            raise ValueError('Invalid handoff notice')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread=self._thread(db,thread_id)
            previous=db.execute('SELECT * FROM dialogue_notices WHERE thread_id=? AND event_key=?',(thread_id,event_key)).fetchone()
            if previous:
                if previous['event_type']!=event_type or previous['summary']!=summary:
                    raise ValueError('Conflicting handoff identity')
                return previous['id']
            if thread['revision']!=expected_revision:
                raise StaleDialogue('Conversation changed during handoff')
            notice_id=uuid.uuid4().hex
            db.execute('INSERT INTO dialogue_notices(id,thread_id,event_key,event_type,summary,created_at) VALUES (?,?,?,?,?,?)',
                       (notice_id,thread_id,event_key,event_type,summary,int(self.clock())))
            if pause and thread['state'] not in {'opt_out','closed'}:
                db.execute("UPDATE dialogue_threads SET state='human',next_contact_at=NULL,revision=revision+1,updated_at=? WHERE id=?",
                           (int(self.clock()),thread_id))
                db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread_id,))
            return notice_id

    def manager_control(self, thread_id, *, actor_account_id, manager_id, command, expected_revision):
        if command not in {'pause','resume'}:
            raise ValueError('Unknown manager control')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread=self._thread(db,thread_id)
            if thread['account_id']!=actor_account_id or thread['manager_id']!=manager_id:
                raise PermissionError('Only the bound manager can control this dialogue')
            if thread['revision']!=expected_revision:
                raise StaleDialogue('Manager button is stale; refresh dialogue')
            if thread['state'] in {'opt_out','closed'}:
                raise ValueError('Stopped client must not be reactivated with this button')
            state='human' if command=='pause' else 'engaged'
            db.execute('UPDATE dialogue_threads SET state=?,next_contact_at=NULL,revision=revision+1,updated_at=? WHERE id=?',
                       (state,int(self.clock()),thread_id))
            db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread_id,))
            db.execute("UPDATE dialogue_notices SET status='handled' WHERE thread_id=? AND status IN ('pending','delivered')",(thread_id,))

    def pending_notices(self, account_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute('''SELECT n.*,t.account_id,t.manager_id,t.peer_id,t.product,
                t.revision,t.state FROM dialogue_notices n JOIN dialogue_threads t ON t.id=n.thread_id
                WHERE t.account_id=? AND n.status='pending' ORDER BY n.created_at,n.id''',(account_id,))]

    def already_notified(self,thread_id,event_type):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM dialogue_notices WHERE thread_id=? AND event_type=? AND status IN ('pending','delivered')",(thread_id,event_type)).fetchone() is not None

    def acknowledge_notice(self, notice_id, *, account_id):
        with self.connection() as db:
            changed=db.execute("""UPDATE dialogue_notices SET status='delivered' WHERE id=? AND status='pending'
                AND thread_id IN (SELECT id FROM dialogue_threads WHERE account_id=?)""",(notice_id,account_id)).rowcount
            if changed!=1:
                raise ValueError('Notice unavailable for this account')

    def reminder_history(self, thread_id):
        with self.connection() as db:
            self._thread(db,thread_id)
            return [r[0] for r in db.execute("SELECT sent_at FROM dialogue_actions WHERE thread_id=? AND kind='reminder' AND status='sent' ORDER BY sent_at,id",(thread_id,))]

    def action_for_key(self, thread_id, action_key):
        with self.connection() as db:
            self._thread(db,thread_id)
            row=db.execute('SELECT * FROM dialogue_actions WHERE thread_id=? AND action_key=?',(thread_id,action_key)).fetchone()
            return dict(row) if row else None

    def threads(self, account_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM dialogue_threads WHERE account_id=?',(account_id,))]

    def recover_failed_reminders(self, *, account_id,manager_id,dry_run=False):
        """Recover only untouched infrastructure-era reminder pauses, never manual/client stops."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('''SELECT t.* FROM dialogue_threads t WHERE t.account_id=? AND t.manager_id=?
                AND t.state='human' AND t.history_complete=1
                AND EXISTS(SELECT 1 FROM dialogue_notices n WHERE n.thread_id=t.id
                    AND n.event_type='knowledge_missing' AND n.event_key LIKE 'knowledge_missing:reminder:%'
                    AND n.status IN ('pending','delivered'))
                AND NOT EXISTS(SELECT 1 FROM dialogue_notices n WHERE n.thread_id=t.id
                    AND (n.event_type!='knowledge_missing' OR n.status='handled'))
                AND NOT EXISTS(SELECT 1 FROM dialogue_messages m WHERE m.thread_id=t.id
                    AND m.sent_at>=t.first_sent_at AND (m.direction='incoming' OR m.telegram_id!=t.first_message_id))
                AND NOT EXISTS(SELECT 1 FROM dialogue_actions a WHERE a.thread_id=t.id
                    AND a.status IN ('sending','uncertain','sent'))''',(account_id,manager_id)).fetchall()
            ids=[r['id'] for r in rows]
            if not dry_run:
                for thread_id in ids:
                    db.execute("UPDATE dialogue_threads SET state='waiting',next_contact_at=NULL,revision=revision+1,updated_at=? WHERE id=?",
                               (int(self.clock()),thread_id))
                    db.execute("UPDATE dialogue_notices SET status='handled' WHERE thread_id=? AND event_type='knowledge_missing'",
                               (thread_id,))
            return ids

    def pending_actions(self, account_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute('''SELECT a.* FROM dialogue_actions a
                JOIN dialogue_threads t ON t.id=a.thread_id WHERE t.account_id=? AND a.status='pending'
                AND a.due_at<=? ORDER BY a.due_at,a.created_at''',(account_id,int(self.clock())))]

    def outgoing_action(self, thread_id, telegram_id):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status='sent' AND telegram_id=?",(thread_id,telegram_id)).fetchone() is not None

    def outgoing_text(self,thread_id,telegram_id):
        with self.connection() as db:
            row=db.execute("SELECT text FROM dialogue_actions WHERE thread_id=? AND status='sent' AND telegram_id=?",(thread_id,telegram_id)).fetchone()
            return row[0] if row else None

    def unresolved_actions(self,thread_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM dialogue_actions WHERE thread_id=? AND status IN ('sending','uncertain')",(thread_id,))]

    def client_label(self,thread_id):
        with self.connection() as db:
            thread=self._thread(db,thread_id)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clients'").fetchone():
                row=db.execute('SELECT full_name,username FROM clients WHERE manager_id=? AND telegram_id=? AND product=? ORDER BY id DESC LIMIT 1',
                               (thread['manager_id'],thread['peer_id'],thread['product'])).fetchone()
                if row:
                    return ' '.join(v for v in (row['full_name'],('@'+row['username'].lstrip('@')) if row['username'] else '') if v)
            return 'Telegram ID '+str(thread['peer_id'])
