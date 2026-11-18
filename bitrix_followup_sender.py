"""Review and send assigned Bitrix follow-ups from the manager Telegram session."""

import asyncio
import base64
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


async def center(client, action, job=None):
    import access_runtime
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    authority = os.environ['ACCESS_BOT_USERNAME'].strip().lstrip('@')
    encryption = os.environ['SYNC_PUBLIC_KEY'].strip()
    signing = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'].strip(), validate=True)
    )
    ref = {'id': job['id'], 'digest': event_digest(job)} if job else None
    return await call_center(client, authority, encryption, signing, access_runtime.transport_lock,
                             guard, authorized()['subject'], action, ref)


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
    entity = await resolve(client, job)
    if entity is None:
        return 'пропущено, Telegram-аккаунт не совпал'
    local_job = {**job, 'peer': entity.id}
    try:
        allowed_contact(local_job)
        with get_connection() as connection:
            local_client(connection, local_job)
    except (ContactOptOutError, ValueError):
        return 'пропущено, проверьте контакт'
    permit = await center(client, 'claim_auto' if automatic else 'claim', job)
    if not permit.get('ok') or permit.get('job') != job['id']:
        return 'пропущено, Битрикс не подтвердил отправку или сообщение уже обработано'
    grant = guard()
    if automatic and grant.get('bitrix_followup_auto') is not True:
        raise AccessDenied('Daily send no longer assigned')
    allowed_contact(local_job)
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
