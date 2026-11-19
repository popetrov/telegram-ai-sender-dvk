from finance_scope import account_allowed, thread_allowed, current_account, current_manager
"""Opt-in controls for the owner's first seller pilot. No Telegram or AI calls."""
import json

OWNER_ACCOUNT_ID = 925600480
MODES = {'active_negotiation', 'nurture', 'silent', 'copilot', 'paused_until', 'off'}
MODE_LABELS = {
    'active_negotiation': 'Переговоры', 'nurture': 'Прогрев', 'silent': 'Молчун',
    'copilot': 'Помощник менеджера', 'paused_until': 'Пауза до даты', 'off': 'Выключен',
}
COLUMNS = {
    'control_reason': "TEXT NOT NULL DEFAULT ''",
    'peer_username': "TEXT NOT NULL DEFAULT ''",
    'active_product': 'TEXT',
    'anchor_kind': "TEXT NOT NULL DEFAULT 'outgoing'",
    'seller_enabled': 'INTEGER NOT NULL DEFAULT 0',
    'mode': "TEXT NOT NULL DEFAULT 'off'",
    'outgoing_allowed': 'INTEGER NOT NULL DEFAULT 0',
    'control_epoch': 'INTEGER NOT NULL DEFAULT 0',
    'paused_until': 'INTEGER',
    'resume_pending': 'INTEGER NOT NULL DEFAULT 0',
    'resume_turn_id': 'INTEGER NOT NULL DEFAULT 0',
    'processed_incoming_id': 'INTEGER NOT NULL DEFAULT 0',
    'incoming_floor_id': 'INTEGER NOT NULL DEFAULT 0',
    'copilot_revision': 'INTEGER NOT NULL DEFAULT -1',
}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS dialogue_schema_migrations '
               '(version TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)')
    existing = {r['name'] for r in db.execute('PRAGMA table_info(dialogue_threads)')}
    for column, definition in COLUMNS.items():
        if column not in existing:
            db.execute(f'ALTER TABLE dialogue_threads ADD COLUMN {column} {definition}')
    db.execute('''CREATE TABLE IF NOT EXISTS dialogue_control_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
        event TEXT NOT NULL, actor_account_id INTEGER, revision INTEGER NOT NULL,
        data TEXT NOT NULL, created_at INTEGER NOT NULL)''')
    db.execute("INSERT OR IGNORE INTO dialogue_schema_migrations VALUES ('seller-stage1', strftime('%s','now'))")


def event(db, thread_id, name, revision, now, *, actor=None, **data):
    db.execute('INSERT INTO dialogue_control_events '
               '(thread_id,event,actor_account_id,revision,data,created_at) VALUES (?,?,?,?,?,?)',
               (thread_id, name, actor, revision, json.dumps(data, ensure_ascii=False), int(now)))


def can_send(thread, kind='reply'):
    if not thread['seller_enabled']:
        return True  # Existing dialogue installations keep their behavior.
    return (thread_allowed(thread) and thread['outgoing_allowed'] == 1
            and thread['mode'] in {'active_negotiation', 'nurture', 'silent'}
            and not thread['resume_pending'])


class SellerControls:
    def __init__(self, store):
        self.store = store

    def _bound(self, db, thread_id, actor, manager, revision, *, stopping=False):
        from dialogue_store import StaleDialogue
        thread = self.store._thread(db, thread_id)
        if (not account_allowed(actor) or not thread_allowed(thread) or thread['account_id'] != actor
                or thread['manager_id'] != manager):
            raise PermissionError('Пилот доступен только владельцу и его диалогу.')
        if revision>thread['revision'] or (not stopping and thread['revision']!=revision):
            raise StaleDialogue('Диалог изменился. Обновите кнопки.')
        if not stopping and (thread['state'] in {'opt_out', 'closed'} or db.execute(
                "SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status IN ('sending','uncertain')", (thread_id,)).fetchone()):
            raise ValueError('Есть запрет клиента, удалённая история или непроверенная отправка.')
        return thread

    def enable(self, thread_id, *, actor, manager, revision):
        """Explicit per-chat permission; never bulk enrollment or automatic resumption."""
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread = self._bound(db, thread_id, actor, manager, revision)
            if thread['seller_enabled']:
                return
            db.execute("UPDATE dialogue_threads SET seller_enabled=1,mode='copilot',state='human',"
                       "control_reason='Пилот подключён. Автоответы ещё не включены.',"
                       'outgoing_allowed=1,resume_pending=0,control_epoch=control_epoch+1,'
                       'next_contact_at=NULL,revision=revision+1,updated_at=? WHERE id=?',
                       (int(self.store.clock()), thread_id))
            db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'", (thread_id,))
            event(db, thread_id, 'seller_enabled', revision+1, self.store.clock(), actor=actor)

    def set_mode(self, thread_id, mode, *, actor, manager, revision, until=None):
        if mode not in MODES:
            raise ValueError('Неизвестный режим диалога.')
        if mode == 'paused_until':
            if type(until) is not int or until <= self.store.clock():
                raise ValueError('Укажите будущую дату и время паузы.')
        elif until is not None:
            raise ValueError('Срок допустим только для паузы.')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            thread = self._bound(db, thread_id, actor, manager, revision, stopping=mode in {'off','copilot','paused_until'})
            revision=thread['revision']
            from dialogue_calendar import cancel_db
            cancel_db(db,thread_id,self.store.clock(),'Изменён режим диалога: '+mode)
            if not thread['seller_enabled']:
                raise ValueError('Новый пилот для этого чата не включён.')
            # All return-to-AI paths first reconcile history while sends stay blocked.
            resume = mode in {'active_negotiation', 'nurture', 'silent'}
            reason=('Менеджер вернул диалог ИИ. Проверяется свежая переписка.' if resume else
                    'Вы взяли диалог на себя.' if mode=='copilot' else
                    'Диалог выключен менеджером.' if mode=='off' else 'Менеджер установил паузу.')
            db.execute("UPDATE dialogue_threads SET mode=?,state=CASE WHEN state IN ('opt_out','closed') THEN state ELSE 'human' END,paused_until=?,resume_pending=?,control_reason=?,"
                       'control_epoch=control_epoch+1,next_contact_at=NULL,revision=revision+1,updated_at=? WHERE id=?',
                       (mode, until, int(resume),reason, int(self.store.clock()), thread_id))
            db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'", (thread_id,))
            db.execute("UPDATE dialogue_notices SET status='handled' WHERE thread_id=? AND status IN ('pending','delivered')", (thread_id,))
            event(db, thread_id, 'mode_changed', revision+1, self.store.clock(), actor=actor, mode=mode, until=until)

    def finish_resume(self, thread_id, *, epoch):
        """Called only after a fresh Telegram history scan; old manager answers stay answered."""
        from dialogue_store import StaleDialogue
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            t = self.store._thread(db, thread_id)
            if (not t['seller_enabled'] or not thread_allowed(t)
                    or t['control_epoch'] != epoch or not t['resume_pending']
                    or t['mode'] not in {'active_negotiation', 'nurture', 'silent'}
                    or t['state'] in {'closed', 'opt_out'} or not t['history_complete']):
                raise StaleDialogue('Возврат отменён: управление диалогом изменилось.')
            if db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status IN ('sending','uncertain')", (thread_id,)).fetchone():
                raise StaleDialogue('Сначала проверьте неопределённую отправку.')
            latest = db.execute('SELECT telegram_id,direction FROM dialogue_messages WHERE thread_id=? '
                                'ORDER BY sent_at DESC,telegram_id DESC LIMIT 1', (thread_id,)).fetchone()
            unanswered=False
            if latest and latest['direction']=='incoming':
                key='%reply:'+str(latest['telegram_id'])+':%'
                role='role_ack:'+str(latest['telegram_id']);need='need_ack:'+str(latest['telegram_id'])
                handed=db.execute("SELECT 1 FROM dialogue_notices WHERE thread_id=? AND event_key LIKE ? "
                    "AND event_type IN ('need','ready','call','documents','knowledge_missing')",(thread_id,key)).fetchone()
                delivered=db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND (action_key LIKE ? OR action_key=? OR action_key=?) "
                    "AND status IN ('sent','sending','uncertain')",(thread_id,key,role,need)).fetchone()
                cancelled=db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND (action_key LIKE ? OR action_key=? OR action_key=?) "
                    "AND status IN ('cancelled','not_sent')",(thread_id,key,role,need)).fetchone()
                # Explicit return also revisits a turn incorrectly marked processed
                # without an outgoing action (e.g. a classifier chose reply_required=false).
                unanswered=bool(not delivered and latest['telegram_id']>t['incoming_floor_id']
                    and (handed or cancelled or latest['telegram_id']<=t['processed_incoming_id']))
                if unanswered:
                    db.execute('UPDATE dialogue_threads SET processed_incoming_id=MIN(processed_incoming_id,?),resume_turn_id=? WHERE id=?',
                               (latest['telegram_id']-1,latest['telegram_id'],thread_id))
            if not unanswered:db.execute('UPDATE dialogue_threads SET resume_turn_id=0 WHERE id=?',(thread_id,))
            reason=('Ошибка календаря исправлена; свежая переписка проверена.'
                    if t['control_reason'].startswith('Восстановление после ошибки календаря 2.0.45.')
                    else 'Менеджер явно вернул диалог ИИ; переписка проверена.')
            db.execute("UPDATE dialogue_threads SET state='engaged',resume_pending=0,control_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                       (reason,int(self.store.clock()), thread_id))
            event(db, thread_id, 'resume_reconciled', t['revision']+1, self.store.clock())
            return bool(latest and latest['direction']=='incoming' and (unanswered or latest['telegram_id']>t['processed_incoming_id']))

    def correct_false_stop(self,thread_id,*,actor,manager,revision,incoming_id,reason):
        """Explicit owner correction, only with a newer incoming request. No auto resume."""
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
            raise ValueError('Нужно основание исправления.')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            t=self._bound(db,thread_id,actor,manager,revision,stopping=True)
            latest=db.execute('SELECT * FROM dialogue_messages WHERE thread_id=? ORDER BY telegram_id DESC LIMIT 1',(thread_id,)).fetchone()
            stop=db.execute("SELECT MAX(telegram_id) FROM dialogue_actions WHERE thread_id=? AND kind='opt_out_ack' AND status='sent'",(thread_id,)).fetchone()[0]
            if (not t['seller_enabled'] or t['state']!='opt_out' or not latest or not stop
                    or latest['telegram_id']!=incoming_id or latest['direction']!='incoming' or incoming_id<=stop
                    or db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status IN ('sending','uncertain')",(thread_id,)).fetchone()):
                raise ValueError('Исправление требует проверенного нового входящего после ошибочной остановки.')
            db.execute("UPDATE dialogue_threads SET state='human',mode='copilot',resume_pending=0,control_epoch=control_epoch+1,"
                       'revision=revision+1,updated_at=? WHERE id=?',(int(self.store.clock()),thread_id))
            event(db,thread_id,'false_contact_stop_corrected',revision+1,self.store.clock(),
                  actor=actor,incoming_id=incoming_id,reason=reason)
