"""Review and send assigned Bitrix follow-ups from the manager Telegram session."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import secrets
import time

from access_lease import AccessDenied
from bitrix_followup_transport import call_center
from sync_protocol import event_digest


PREVIEW = 'bitrix_followup_preview'
log = logging.getLogger(__name__)


def authorized():
    import access_runtime
    import config

    grant = access_runtime.require_access()
    if (grant.get('bitrix_followup') is not True or access_runtime.account_id is None
            or grant['subject'] != f'{config.MANAGER_ID}:{access_runtime.account_id}'):
        raise AccessDenied('Очередь из Битрикса не назначена этому аккаунту.')
    return grant


def guard():
    from bot import require_send_cooldown_finished

    grant = authorized()
    require_send_cooldown_finished()
    return grant


async def center(client, action, job=None, *, request_id=None):
    import access_runtime
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    authority = os.environ['ACCESS_BOT_USERNAME'].strip().lstrip('@')
    encryption = os.environ['SYNC_PUBLIC_KEY'].strip()
    signing = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'].strip(), validate=True)
    )
    ref = {'id': job['id'], 'digest': event_digest(job)} if job else None
    return await call_center(client, authority, encryption, signing, access_runtime.transport_lock,
                             guard, authorized()['subject'], action, ref, request_id=request_id)


def automatic_day():
    local = datetime.now(timezone(timedelta(hours=4)))
    if local.weekday() < 5 and (9, 30) <= (local.hour, local.minute) < (18, 0):
        return local.date().isoformat()
    return None


def permit_journal(db):
    db.execute("""CREATE TABLE IF NOT EXISTS warmup_permits (
               subject TEXT NOT NULL, day TEXT NOT NULL, job_id TEXT NOT NULL,
               request_id TEXT NOT NULL, job TEXT NOT NULL,
               PRIMARY KEY (subject, day, job_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS bitrix_followup_dispatch (
               job_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id))""")


def pending_permits(subject):
    from database import get_connection

    with get_connection() as db:
        permit_journal(db)
        rows = db.execute("""SELECT p.job FROM warmup_permits p
                             WHERE p.subject=? AND p.day=? AND NOT EXISTS (
                               SELECT 1 FROM bitrix_followup_dispatch d WHERE d.job_id=p.job_id)
                             ORDER BY p.rowid""",
                          (subject, automatic_day())).fetchall()
    return [json.loads(row[0]) for row in rows]


def remember_permit(subject, day, job):
    from database import get_connection

    with get_connection() as db:
        permit_journal(db)
        db.execute("INSERT OR IGNORE INTO warmup_permits VALUES (?,?,?,?,?)",
                   (subject, day, job['id'], secrets.token_hex(16), json.dumps(job)))
        return db.execute("""SELECT request_id,job FROM warmup_permits
                             WHERE subject=? AND day=? AND job_id=?""",
                          (subject, day, job['id'])).fetchone()


def forget_permit(subject, day, job_id):
    from database import get_connection

    with get_connection() as db:
        db.execute("DELETE FROM warmup_permits WHERE subject=? AND day=? AND job_id=?",
                   (subject, day, job_id))


def publish_receipts(subject):
    from database import get_connection
    from incident_monitor import record

    with get_connection() as db:
        permit_journal(db)
        receipts = db.execute("""SELECT p.day,p.job_id,p.request_id,m.telegram_message_id,c.telegram_id
                                  FROM warmup_permits p
                                  JOIN bitrix_followup_dispatch d ON d.job_id=p.job_id
                                  JOIN messages m ON m.id=d.message_id
                                  JOIN clients c ON c.id=m.client_id
                                 WHERE p.subject=? AND m.status='sent'
                                   AND m.telegram_message_id IS NOT NULL""", (subject,)).fetchall()
    for day, job_id, request_id, message_id, peer_id in receipts:
        if record('monitor_warning', source='finance_warmup_receipt', request_id=request_id,
                  operation=job_id, message_ids=[int(message_id)], peer_id=int(peer_id)):
            forget_permit(subject, day, job_id)


def confirmed_message_id(audit, local_id, peer):
    rows = audit.execute("""
        SELECT DISTINCT CAST(message.value AS INTEGER)
          FROM monitor_events origin
          JOIN monitor_events result
            ON json_extract(origin.data,'$.evidence.op_id') =
               json_extract(result.data,'$.evidence.op_id')
           AND json_extract(origin.data,'$.run_id') = json_extract(result.data,'$.run_id')
          JOIN json_each(result.data,'$.message_ids') message
         WHERE json_extract(origin.data,'$.kind') = 'forensic_operation'
           AND json_extract(origin.data,'$.operation') = 'SendMessageRequest'
           AND json_extract(origin.data,'$.evidence.origin_table') = 'messages'
           AND json_extract(origin.data,'$.evidence.origin_id') = ?
           AND json_extract(result.data,'$.kind') = 'send_result'
           AND json_extract(result.data,'$.peer_id') = ?
           AND message.type = 'integer' AND message.value > 0
         LIMIT 2
    """, (local_id, peer)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


async def reconcile_receipts(client, subject):
    from contextlib import closing
    from database import get_connection, update_message_status
    from incident_monitor import _default

    with get_connection() as db:
        permit_journal(db)
        rows = db.execute("""SELECT m.id,c.telegram_id,p.job
                               FROM warmup_permits p
                               JOIN bitrix_followup_dispatch d ON d.job_id=p.job_id
                               JOIN messages m ON m.id=d.message_id
                               JOIN clients c ON c.id=m.client_id
                              WHERE p.subject=? AND p.day=?
                                AND m.status IN ('sending','uncertain')
                                AND m.telegram_message_id IS NULL""",
                          (subject, automatic_day())).fetchall()
    if not rows:
        return
    with closing(_default.connect()) as audit:
        candidates = [(local_id, int(peer), json.loads(job),
                       confirmed_message_id(audit, local_id, int(peer)))
                      for local_id, peer, job in rows if peer]
    for local_id, peer, job, message_id in candidates:
        if message_id is None:
            continue
        try:
            async with asyncio.timeout(10):
                entity = await resolve(client, job)
                if entity is None or entity.id != peer:
                    continue
                message = await client.get_messages(entity, ids=message_id)
        except TimeoutError:
            continue
        if (getattr(message, 'id', None) == message_id and getattr(message, 'out', False)
                and getattr(getattr(message, 'peer_id', None), 'user_id', None) == peer):
            update_message_status(local_id, 'sent', telegram_message_id=message_id)


async def resolve(client, job):
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    result = await client(ResolveUsernameRequest(job['username']))
    peer = getattr(result.peer, 'user_id', None)
    if type(peer) is not int or (job.get('peer') is not None and peer != job['peer']):
        return None
    return next((user for user in result.users if user.id == peer
                 and not getattr(user, 'bot', False) and not getattr(user, 'deleted', False)), None)


def allowed_contact(job):
    import config
    from dialogue_store import require_contact_allowed

    require_contact_allowed(config.DATABASE_PATH, manager_id=config.MANAGER_ID, peer_id=job['peer'])


def local_client(connection, job):
    import config

    rows = connection.execute(
        """SELECT id, inn, telegram_id FROM clients WHERE manager_id = ?
           AND (inn = ? OR telegram_id = ? OR lower(ltrim(username, '@')) = lower(?))""",
        (config.MANAGER_ID, job['inn'], job['peer'], job['username']),
    ).fetchall()
    if rows and (len(rows) != 1 or rows[0]['inn'] != job['inn']
                 or rows[0]['telegram_id'] != job['peer']):
        raise ValueError('Контакт в локальной базе требует сверки.')
    return rows[0]['id'] if rows else None


def reserve(job):
    import config
    from database import get_connection, utc_now

    now = utc_now()
    with get_connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            """CREATE TABLE IF NOT EXISTS bitrix_followup_dispatch (
               job_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id))"""
        )
        if connection.execute('SELECT 1 FROM bitrix_followup_dispatch WHERE job_id = ?',
                              (job['id'],)).fetchone():
            return None
        client_id = local_client(connection, job)
        if client_id is None:
            client_id = connection.execute(
                """INSERT INTO clients
                   (inn, full_name, username, telegram_id, product, status, created_at, updated_at, manager_id)
                   VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
                (job['inn'], job['company'], job['username'], job['peer'], job['product'], now, now, config.MANAGER_ID),
            ).lastrowid
        message_id = connection.execute(
            "INSERT INTO messages (client_id, text, status, created_at) VALUES (?, ?, 'sending', ?)",
            (client_id, job['text'], now),
        ).lastrowid
        connection.execute('INSERT INTO bitrix_followup_dispatch VALUES (?, ?)', (job['id'], message_id))
        return message_id


async def dispatch(client, job, *, automatic=False):
    from database import get_connection, update_message_status
    from dialogue_store import ContactOptOutError
    from forensic_trace import origin

    guard()
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError
    try:
        async with asyncio.timeout(20):
            entity = await resolve(client, job)
    except TimeoutError:
        if not automatic:
            raise
        log.warning('Daily contact deferred before reservation (address check timeout)')
        return 'отложено, не удалось проверить адрес'
    except (UsernameNotOccupiedError, UsernameInvalidError):
        # This is a resolution failure, before claim/reservation/send. Other jobs may proceed.
        log.warning('Daily contact skipped before send (invalid username)')
        return 'пропущено, username недоступен'
    if entity is None:
        return 'пропущено, Telegram-аккаунт не совпал'
    local_job = {**job, 'peer': entity.id}
    try:
        allowed_contact(local_job)
        with get_connection() as connection:
            local_client(connection, local_job)
    except (ContactOptOutError, ValueError):
        return 'пропущено, проверьте контакт'
    day = automatic_day() if automatic else None
    subject = authorized()['subject'] if automatic else None
    if automatic and day is None:
        return 'пропущено, окно отправки закрыто'
    request_id = None
    if automatic:
        request_id, original = remember_permit(subject, day, job)
        if json.loads(original) != job:
            return 'отложено, задание изменилось'
    try:
        permit = await center(client, 'claim_auto' if automatic else 'claim', job,
                              request_id=request_id)
    except TimeoutError:
        if not automatic:
            raise
        return 'отложено, центр не подтвердил резервирование'
    if not permit.get('ok') or permit.get('job') != job['id']:
        return 'пропущено, Битрикс не подтвердил отправку или сообщение уже обработано'
    grant = guard()
    if automatic and grant.get('bitrix_followup_auto') is not True:
        raise AccessDenied('Daily send no longer assigned')
    allowed_contact(local_job)
    if automatic and automatic_day() != day:
        return 'пропущено, окно отправки закрыто'
    message_id = reserve(local_job)
    if message_id is None:
        return 'уже обработано'
    from finance_priority import begin_external_send, finish_external_send
    receipt = begin_external_send(entity.id)
    try:
        with origin('campaign', 'messages', message_id):
            sent = await client.send_message(entity, job['text'], parse_mode=None)
        if not getattr(sent, 'id', None):
            raise ValueError('Telegram не вернул ID сообщения')
        update_message_status(message_id, 'sent', telegram_message_id=sent.id)
    except (Exception, asyncio.CancelledError) as exc:
        update_message_status(message_id, 'uncertain', send_error=type(exc).__name__)
        raise
    finally:
        finish_external_send(entity.id, receipt)
    if automatic:
        try:
            publish_receipts(subject)
        except Exception as exc:
            log.warning('Warm-up receipt telemetry deferred (%s)', type(exc).__name__)
    return 'отправлено'


def pause_if_telegram_requires(exc):
    from bot import pause_for_flood, pause_for_spam
    from telethon.errors import FloodWaitError, PeerFloodError

    if isinstance(exc, FloodWaitError):
        pause_for_flood(exc)
    elif isinstance(exc, PeerFloodError):
        pause_for_spam()


def permitted_user(update):
    import access_runtime

    account = access_runtime.account_id
    if (update.effective_user is None or update.effective_user.id != account
            or update.effective_chat is None or update.effective_chat.type != 'private'
            or update.effective_chat.id != account):
        return False
    try:
        authorized()
    except AccessDenied:
        return False
    return True


async def show(application, client, *, startup=False):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    import access_runtime

    if client is None:
        return
    if startup:
        await access_runtime.wait_initial_access()
    try:
        grant = authorized()
    except AccessDenied:
        return
    account = access_runtime.account_id
    if grant.get('bitrix_followup_auto') is True:
        if not startup:
            await application.bot.send_message(account, 'Очередь из Битрикса обрабатывается автоматически.')
        return
    application.bot_data.pop(PREVIEW, None)
    try:
        guard()
        result = await center(client, 'list')
        if not result.get('ok'):
            raise PermissionError('Центр не подтвердил доступ к очереди.')
        jobs = result['jobs']
        if not jobs:
            if not startup:
                await application.bot.send_message(account, 'В очереди из Битрикса нет сообщений.')
            return
        token = secrets.token_hex(6)
        preview = '\n\n'.join(
            f"{job['company']} · @{job['username']} · {job['product']}\n{job['text']}"
            for job in jobs
        )
        message = await application.bot.send_message(
            account, f'Черновики из Битрикса ({len(jobs)}). Проверьте получателей и тексты.\n\n{preview}',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('Отправить после проверки', callback_data=f'bitrix_pilot_{token}')
            ]]),
        )
        application.bot_data[PREVIEW] = (token, time.monotonic() + 900, jobs, message.message_id)
    except Exception as exc:
        pause_if_telegram_requires(exc)
        log.warning('Bitrix follow-up preview stopped (%s)', type(exc).__name__)
        if not startup:
            await application.bot.send_message(account, 'Очередь из Битрикса недоступна. Попробуйте /bitrix_queue позже.')


async def queue_command(update, context):
    import bot

    if permitted_user(update):
        await show(context.application, bot.telegram_client)


async def confirm(update, context):
    import bot

    query = update.callback_query
    if not permitted_user(update):
        await query.answer()
        return
    data = context.application.bot_data
    current = data.get(PREVIEW)
    if (current is None or query.data != f'bitrix_pilot_{current[0]}' or time.monotonic() >= current[1]
            or query.message is None or query.message.message_id != current[3]):
        await query.answer('Черновик устарел. Откройте /bitrix_queue.', show_alert=True)
        return
    if bot.queue_is_active(context):
        await query.answer('Дождитесь завершения другой очереди и нажмите снова.', show_alert=True)
        return
    data.pop(PREVIEW)
    prior = data.get(bot.QUEUE_KEY)
    slot = {'state': 'sending', 'source': 'bitrix_followup'}
    data[bot.QUEUE_KEY] = slot
    results = []
    try:
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            log.warning('Could not clear Bitrix follow-up button (%s)', type(exc).__name__)
        for index, job in enumerate(current[2]):
            try:
                if bot.delivery_gate is not None:
                    status = await bot.delivery_gate.run(lambda: dispatch(bot.telegram_client, job), guard)
                else:
                    if index:
                        await asyncio.sleep(bot.SEND_INTERVAL_SECONDS)
                    status = await dispatch(bot.telegram_client, job)
                results.append(f"{job['company']} — {status}")
            except Exception as exc:
                pause_if_telegram_requires(exc)
                log.warning('Bitrix follow-up stopped (%s)', type(exc).__name__)
                results.append(f"{job['company']} — отправка остановлена, проверьте статус в Telegram")
                results.extend(f"{pending['company']} — не отправлялось" for pending in current[2][index + 1:])
                break
        await query.message.reply_text('\n'.join(results))
    finally:
        if data.get(bot.QUEUE_KEY) is slot:
            if prior is None:
                data.pop(bot.QUEUE_KEY, None)
            else:
                data[bot.QUEUE_KEY] = prior


def register(application):
    from telegram.ext import CallbackQueryHandler, CommandHandler

    application.add_handler(CommandHandler('bitrix_queue', queue_command))
    application.add_handler(CallbackQueryHandler(confirm, pattern=r'^bitrix_pilot_[a-f0-9]{12}$'))
