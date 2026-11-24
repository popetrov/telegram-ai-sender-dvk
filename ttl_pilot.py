"""Finance pilot, requiring an authenticated request in the local manager bot."""
import asyncio
import hashlib
import hmac
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timedelta, timezone

from access_lease import AccessDenied
from telegram.ext import CommandHandler
from telethon import functions, types, utils

from ttl_pilot_targets import TARGETS


def scope(account_id):
    import access_runtime
    import config
    from primary_identity import require_current
    grant = access_runtime.require_access()
    subject = f"{config.MANAGER_ID}:{account_id}"
    if grant.get("subject") != subject or grant.get("update_channel", {}).get("name") != "dvk-finance":
        raise PermissionError("Finance pilot assignment required")
    role = hashlib.sha256(subject.encode()).hexdigest()
    if role not in TARGETS:
        raise PermissionError("Not in the timer pilot")
    require_current(config.DATABASE_PATH)
    path = Path(config.DATABASE_PATH).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        values = dict(db.execute("SELECT key,value FROM local_settings WHERE key IN "
                                 "('manager_id','account_id','data_store_id')"))
    if (values.get("manager_id") != config.MANAGER_ID or values.get("account_id") != str(account_id)
            or not re.fullmatch("[0-9a-f]{32}", values.get("data_store_id") or "")):
        raise PermissionError("Incorrect or unbound local database")
    return bytes.fromhex(values["data_store_id"]), TARGETS[role]


def is_target(peer_id, key, targets):
    return targets is not None and hmac.new(key, str(peer_id).encode(), "sha256").hexdigest() in targets


async def timer(client, peer):
    response = await client(functions.users.GetFullUserRequest(utils.get_input_user(peer)),
                            flood_sleep_threshold=0)
    value = response.full_user.ttl_period
    return 0 if value is None else value


def register(application, client, account_id):
    lock = asyncio.Lock()

    async def inspect(update, context):
        if (update.effective_user is None or update.effective_chat is None
                or update.effective_user.id != account_id or update.effective_chat.type != "private"
                or update.effective_chat.id != account_id):
            return
        async with lock:
            try:
                actual = await client.get_me()
                if actual.id != account_id:
                    raise PermissionError("Telegram account mismatch")
                key, targets = scope(account_id)
            except (AccessDenied, ValueError, PermissionError, sqlite3.Error):
                await update.effective_message.reply_text("Проверка таймеров для этой установки недоступна.")
                return
            change = targets is not None and context.args == ["off"]
            summary = []
            global_value = "не удалось прочитать"
            try:
                result = await client(functions.messages.GetDefaultHistoryTTLRequest(),
                                      flood_sleep_threshold=0)
                global_value = f"{result.period} с" if result.period else "выключен"
                from incident_monitor import record
                record("monitor_warning", reason_code="ttl_snapshot", source="account_default_ttl",
                       count=result.period or 0)
            except Exception:
                pass
            summary.append(f"Общий таймер: {global_value}")
            active_since = datetime.now(timezone.utc) - timedelta(days=30)
            matched = set()
            try:
                async for dialog in client.iter_dialogs():
                    user = dialog.entity
                    if not isinstance(user, types.User) or user.bot or user.is_self or user.deleted:
                        continue
                    if targets is None and (dialog.date is None or dialog.date < active_since):
                        continue
                    if targets is not None and not is_target(user.id, key, targets):
                        continue
                    if user.id in matched:
                        continue
                    matched.add(user.id)
                    try:
                        peer = utils.get_input_peer(user)
                        value = await timer(client, peer)
                        status = f"включён ({value} с)" if value else "уже выключен"
                        if change and value > 0:
                            await client.disable_pilot_timer(peer, account_id)
                            value = await timer(client, peer)
                            status = "отключён и проверен" if value == 0 else "остался включён"
                        from incident_monitor import record
                        record("monitor_warning", reason_code="ttl_snapshot", source="chat_history_ttl",
                               peer_id=user.id, count=value)
                    except Exception as error:
                        status = "не подтверждено (" + type(error).__name__ + ")"
                    name = dialog.name or "Диалог без имени"
                    summary.append(f"{name[:80]} — {status}")
                    if targets is not None and len(matched) == len(targets):
                        break
            except Exception as error:
                summary.append("Список чатов прочитан не полностью (" + type(error).__name__ + ").")
            if targets is not None:
                summary.append(f"Найдено в Telegram: {len(matched)} из {len(targets)}.")
                if not change:
                    summary.append("Чтобы отключить таймеры в найденных чатах, отправьте /ttl off.")
            for offset in range(0, len(summary), 25):
                await update.effective_message.reply_text("\n".join(summary[offset:offset + 25]))

    application.add_handler(CommandHandler("ttl", inspect))
