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

from access_lease import AccessDenied, LeaseStore, POLL_SECONDS

_store = None
account_id = None
_log = logging.getLogger(__name__)
transport_lock = asyncio.Lock()


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
        await conversation.send_message("/lease " + encoded)
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
    me = await client.get_me()
    account_id = me.id
    _store = LeaseStore(Path("data/access_lease.json"),
                        base64.b64decode(public, validate=True), f"{manager}:{me.id}")

    async def poll():
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
