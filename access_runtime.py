"""Manager-side access checks over the existing authorized Telegram account."""
import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

from access_lease import AccessDenied, LeaseStore, POLL_SECONDS, canonical

_store = None
account_id = None
_log = logging.getLogger(__name__)
transport_lock = asyncio.Lock()


async def cleanup_service_messages(client, authority, *messages):
    ids = [m.id for m in messages if type(getattr(m, 'id', None)) is int]
    if not ids:
        return
    try:
        async with asyncio.timeout(10):
            await client.delete_messages(authority, ids, revoke=True)
    except Exception as exc:
        # Cleanup must never revoke a saved lease or force a data retransmission.
        _log.warning('Service chat cleanup unavailable (%s)', type(exc).__name__)


def is_service_message(message, store):
    text = message.raw_text or ''
    if message.out:
        if text == 'DVK_SYNC_V1' and getattr(getattr(message, 'file', None), 'name', None) == 'encrypted.dvk':
            return True
        if text.startswith('/lease '):
            try:
                request = json.loads(base64.urlsafe_b64decode(text[7:]))
                return request.get('subject') == store.subject
            except (ValueError, TypeError):
                return False
        return False
    for prefix, protocol in [('DVK_ACCESS ', None), ('DVK_RECEIPT ', 'DVK_RECEIPT_V1')]:
        if not text.startswith(prefix):
            continue
        try:
            envelope = json.loads(text[len(prefix):])
            payload = envelope['payload']
            store.key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(payload))
            return (payload.get('subject') == store.subject
                    and (payload.get('version') == 1 if protocol is None else payload.get('protocol') == protocol))
        except Exception:
            return False
    return False


async def clean_service_history(client, authority, store):
    # Restricted to this configured bot and this installation's service traffic.
    # Pending uploads remain in SQLite and will be retried if an old file is removed.
    try:
        async with asyncio.timeout(45):
            batch = []
            async for message in client.iter_messages(authority, limit=2000):
                if is_service_message(message, store):
                    batch.append(message)
                if len(batch) == 100:
                    await cleanup_service_messages(client, authority, *batch)
                    batch = []
            await cleanup_service_messages(client, authority, *batch)
    except Exception as exc:
        _log.warning('Old service chat cleanup unavailable (%s)', type(exc).__name__)


def require_access():
    if _store is None:
        raise AccessDenied("Центральный доступ не настроен или ещё не проверен.")
    return _store.require_access()


async def refresh(client, authority, store):
    nonce = secrets.token_hex(24)
    request = json.dumps({"subject": store.subject, "nonce": nonce}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(request.encode()).decode()
    # Resolve the configured bot, then accept messages from that peer only.
    async with client.conversation(authority, timeout=30, exclusive=True) as conversation:
        sent = await conversation.send_message("/lease " + encoded, silent=True)
        # An earlier timed-out response may still be arriving. Only this nonce
        # is accepted; unrelated messages do not replace the cached grant.
        async with asyncio.timeout(30):
            while True:
                response = await conversation.get_response()
                text = response.raw_text or ""
                if not text.startswith("DVK_ACCESS "):
                    continue
                try:
                    envelope = json.loads(text[len("DVK_ACCESS "):])
                    if envelope.get("payload", {}).get("nonce") != nonce:
                        continue
                    store.accept(envelope, nonce)
                    await cleanup_service_messages(client, authority, sent, response)
                    return
                except (ValueError, TypeError, AttributeError, AccessDenied):
                    _log.warning("Invalid central access response; cached lease unchanged")


async def start(client):
    global _store, account_id
    authority = os.getenv("ACCESS_BOT_USERNAME", "").strip().lstrip("@")
    public = os.getenv("ACCESS_PUBLIC_KEY", "").strip()
    manager = os.getenv("MANAGER_ID", "").strip()
    if not authority or not public or not manager:
        raise RuntimeError("Задайте ACCESS_BOT_USERNAME, ACCESS_PUBLIC_KEY и MANAGER_ID в .env.")
    if authority.casefold() == 'asdvk2026_bot':
        raise RuntimeError('Перенесите обмен в технического бота: python manager_setup.py --service-bot dvk_sync2026_bot')
    me = await client.get_me()
    account_id = me.id
    _store = LeaseStore(Path("data/access_lease.json"),
                        base64.b64decode(public, validate=True), f"{manager}:{me.id}")

    async def poll():
        async with transport_lock:
            await clean_service_history(client, authority, _store)
        while True:
            started = time.monotonic()
            try:
                async with transport_lock:
                    await refresh(client, authority, _store)
            except Exception as exc:
                _log.warning("Central access check unavailable (%s); using cached permission",
                             type(exc).__name__)
            await asyncio.sleep(max(0, POLL_SECONDS - (time.monotonic() - started)))

    task = asyncio.create_task(poll(), name="central-access")
    # Run the first check in the background so /start and /request_access stay
    # available even when the authority is offline. No grant means no work.
    return task


async def stop(task):
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
