"""Complete missing historic identities and initialize only new calendars."""
import time


async def restore_legacy_identities(client, manager, attempted):
    from database import get_connection
    from finance_scope import require_access
    from dialogue_runtime import original_text_matches
    require_access()
    with get_connection() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT c.id client_id,m.telegram_message_id,m.text FROM clients c "
            "JOIN messages m ON m.client_id=c.id WHERE c.manager_id=? "
            "AND (c.telegram_id IS NULL OR c.telegram_id<=0) "
            "AND c.product IN ('БГ','Кредит') AND m.status='sent' "
            "AND m.telegram_message_id>0 ORDER BY m.id DESC", (manager,))
                if r['telegram_message_id'] not in attempted][:20]
    if not rows:
        return
    messages = await client.get_messages(None, ids=[r['telegram_message_id'] for r in rows])
    found = {m.id: m for m in messages if m is not None}
    require_access()
    with get_connection() as db:
        for row in rows:
            attempted.add(row['telegram_message_id'])
            message = found.get(row['telegram_message_id'])
            if (message is None or not getattr(message, 'out', False)
                    or type(message.peer_id).__name__ != 'PeerUser'
                    or not original_text_matches(message.raw_text, row['text'])):
                continue
            db.execute('UPDATE clients SET telegram_id=? WHERE id=? AND manager_id=? '
                       'AND (telegram_id IS NULL OR telegram_id<=0)',
                       (message.peer_id.user_id, row['client_id'], manager))


def seed_calendar(worker):
    from dialogue_calendar import assign, classify, initialize
    store, tid = worker.store, worker.thread_id
    initialize(store)
    thread, history = store.snapshot(tid)
    if (not thread['seller_enabled'] or not thread['history_complete']
            or thread['resume_pending'] or thread['state'] in {'human', 'closed', 'opt_out'}):
        return
    with store.connection() as db:
        if (db.execute('SELECT 1 FROM seller_warm_cohorts WHERE thread_id=?', (tid,)).fetchone()
                or db.execute('SELECT 1 FROM seller_followups WHERE thread_id=?', (tid,)).fetchone()):
            return
    if any(m['direction'] == 'incoming' and m['telegram_id'] > thread['processed_incoming_id']
           for m in history):
        return
    category, _ = classify(store, thread, history)
    if category not in {'silent', 'conversation'}:
        return
    assign(store, tid, category, actor=thread['account_id'], manager=thread['manager_id'],
           revision=thread['revision'], live=True)
