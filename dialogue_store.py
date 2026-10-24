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

    def initialize(self):
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
            ''')

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
            if source == 'manager' and state != 'opt_out':
                state = 'human'
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
            changed=db.execute("UPDATE dialogue_actions SET status=?,telegram_id=? WHERE id=? AND status='sending'",
                               (status,telegram_id,action_id)).rowcount
            if changed!=1:
                raise ValueError('Action was not reserved or already completed')
