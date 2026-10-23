"""Run on the administrator's PC with a dedicated Telegram bot token.

No public IP or inbound port is required. The signing key must never be
shipped to manager installations. See CENTRAL_ACCESS.md before deployment.
"""
import base64
import asyncio
import contextlib
import json
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from access_lease import issue
from authority_store import AuthorityStore

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data/access_authority"


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_registry():
    path = STATE / "users.json"
    if not path.exists():
        return {}
    registry = json.loads(path.read_text("utf-8"))
    if not isinstance(registry, dict):
        raise ValueError("Invalid access registry; restore from backup")
    return registry


def authorized_request(registry, account_id, subject):
    record = registry.get(str(account_id), {})
    return record.get("allowed") is True and record.get("subject") == subject


def initialize_key():
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "signing.key"
    if path.exists():
        key = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        # Exclusive creation avoids replacing a concurrent or existing key.
        with path.open("xb") as stream:
            stream.write(raw)
        if os.name != "nt":
            path.chmod(0o600)
    public = base64.b64encode(key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)).decode()
    (STATE / "public_key.txt").write_text(public + "\n", encoding="ascii")
    return key, public


def create_server(token, admin_id, key, store=None, sync_key=None):
    application = Application.builder().token(token).build()

    async def lease(update, context):
        if update.effective_chat.type != "private":
            return
        try:
            if len(context.args) != 1 or len(context.args[0]) > 1500:
                raise ValueError("Invalid request")
            request = json.loads(base64.b64decode(context.args[0], altchars=b"-_", validate=True))
            subject, nonce = request["subject"], request["nonce"]
            account_id = update.effective_user.id
            if (not isinstance(subject, str) or not subject.endswith(f":{account_id}")
                    or len(subject) > 200 or not isinstance(nonce, str)
                    or len(nonce) != 48 or any(c not in "0123456789abcdef" for c in nonce)):
                raise ValueError("Invalid identity or nonce")
            subscription_end = None
            if store is not None:
                new_request = not any(r['account_id'] == account_id for r in store.list_managers())
                store.register(account_id, subject)
                allowed, subscription_end = store.permission(account_id, subject)
            else:
                registry = load_registry()
                record = registry.get(str(account_id))
                new_request = record is None
                if new_request:
                    registry[str(account_id)] = {"subject": subject, "allowed": False}
                    save_json(STATE / "users.json", registry)
                allowed = authorized_request(registry, account_id, subject)
            envelope = issue(key, subject=subject, nonce=nonce, allowed=allowed,
                             subscription_end=subscription_end)
            await update.effective_message.reply_text(
                "DVK_ACCESS " + json.dumps(envelope, separators=(",", ":")))
            if new_request:
                await context.bot.send_message(admin_id,
                    f"Новая установка: Telegram ID {account_id}\n"
                    f"Идентификатор: {subject}\n"
                    f"Разрешить: /approve {account_id}\nОтключить: /revoke {account_id}")
        except (ValueError, KeyError, TypeError):
            await update.effective_message.reply_text("Некорректная заявка доступа.")

    async def decision(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != "private":
            return
        if len(context.args) < 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Формат: /approve ID КОМПАНИЯ или /revoke ID")
            return
        if store is not None:
            try:
                target = int(context.args[0])
                if update.effective_message.text.split()[0].split('@')[0] == '/approve':
                    if len(context.args) != 2:
                        raise ValueError('Формат: /approve ID КОМПАНИЯ (например dvk)')
                    store.assign(target, context.args[1])
                else:
                    store.revoke(target)
                await update.effective_message.reply_text('Решение сохранено. Применится при следующей проверке.')
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
            return
        registry = load_registry()
        target = str(int(context.args[0]))
        if target not in registry:
            await update.effective_message.reply_text("Сначала установка должна отправить запрос доступа.")
            return
        allowed = update.effective_message.text.split()[0].split("@")[0] == "/approve"
        registry[target]["allowed"] = allowed
        save_json(STATE / "users.json", registry)
        await update.effective_message.reply_text(
            f"{target}: " + ("доступ разрешён" if allowed else "доступ отозван")
            + ". Применится при следующей успешной проверке.")

    async def status(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != "private":
            return
        if store is not None:
            await update.effective_message.reply_text(
                'Центр работает.\n/users — менеджеры\n/companies — компании\n'
                '/company ID — создать или включить\n/company_off ID — отключить компанию\n'
                '/renew ID — продлить на месяц + 2 дня\n/approve ID КОМПАНИЯ\n/revoke ID\n'
                'Часовой пояс: Asia/Dubai. dvk — бессрочно.')
            return
        registry = load_registry()
        await update.effective_message.reply_text(
            "Центр доступа работает. /approve ID — разрешить; /revoke ID — отключить.\n"
            f"Установок: {len(registry)}, разрешено: "
            f"{sum(r.get('allowed') is True for r in registry.values())}")

    async def users(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != "private":
            return
        if store is not None:
            lines = [f"{r['account_id']}: {r['company_id'] or 'ожидает'}; включён={bool(r['enabled'])}; {r['subject']}"
                     for r in store.list_managers()]
        else:
            registry = load_registry()
            lines = [f"{account}: {'разрешён' if record.get('allowed') is True else 'отключён/ожидает'} — {record.get('subject', '')}"
                     for account, record in registry.items()]
        # Keep responses within Telegram message size limits.
        for start in range(0, max(1, len(lines)), 10):
            await update.effective_message.reply_text("\n".join(lines[start:start+10]) or "Установок пока нет.")

    async def companies(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != 'private' or store is None:
            return
        command = update.effective_message.text.split()[0].split('@')[0]
        try:
            if command != '/companies':
                if len(context.args) != 1:
                    raise ValueError('Укажите ID компании')
                if command == '/renew':
                    store.renew(context.args[0])
                else:
                    store.company(context.args[0], enabled=command != '/company_off')
            for row in store.list_companies():
                end = ('бессрочно' if row['perpetual'] else datetime.fromtimestamp(
                    row['expires_at'], ZoneInfo('Asia/Dubai')).isoformat() if row['expires_at'] else 'не оплачено')
                await update.effective_message.reply_text(f"{row['id']}: включена={bool(row['enabled'])}; {end}")
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))

    for command in ('company', 'company_off', 'renew', 'companies'):
        application.add_handler(CommandHandler(command, companies))
    if sync_key is not None and store is not None:
        from sync_runtime import register_receiver
        register_receiver(application, store, key, sync_key)
    application.add_handler(CommandHandler("users", users))
    application.add_handler(CommandHandler("lease", lease))
    application.add_handler(CommandHandler("approve", decision))
    application.add_handler(CommandHandler("revoke", decision))
    application.add_handler(CommandHandler("start", status))
    async def on_error(update, context):
        logging.getLogger(__name__).error('Center operation failed (%s)', type(context.error).__name__)
    application.add_error_handler(on_error)
    return application


def main():
    # Separate config: never accidentally poll a manager bot with BOT_TOKEN.
    load_dotenv(ROOT / ".env.authority")
    token = os.getenv("ACCESS_AUTHORITY_BOT_TOKEN", "").strip()
    admin_id = int(os.environ["ACCESS_AUTHORITY_ADMIN_ID"])
    if not token or admin_id <= 0:
        raise RuntimeError("Configure .env.authority; see CENTRAL_ACCESS.md")
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # HTTP request URLs contain the bot token; never log them.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    from sync_protocol import initialize_encryption_key, public_text
    from file_lock import FileLock
    with FileLock(STATE / 'center.lock'):
        key, public = initialize_key()
        print("Public key (safe to distribute): " + public)
        sync_key = initialize_encryption_key(STATE / 'encryption.key')
        (STATE / 'sync_public_key.txt').write_text(public_text(sync_key) + '\n', encoding='ascii')
        print('Sync public key (safe to distribute): ' + public_text(sync_key))
        store = AuthorityStore(STATE / 'authority.sqlite')
        application = create_server(token, admin_id, key, store, sync_key)
        backup_task = None
        async def after_start(app):
            nonlocal backup_task
            if (STATE / 'backup.json').exists():
                from center_backup import loop
                backup_task = asyncio.create_task(loop(STATE, STATE / 'backup.json'))
            print('Центр подключён к Telegram. Откройте @asdvk2026_bot и отправьте /start.', flush=True)
        async def after_stop(app):
            if backup_task:
                backup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await backup_task
        application.post_init = after_start
        application.post_shutdown = after_stop
        application.run_polling()


if __name__ == "__main__":
    main()
