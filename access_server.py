"""Run on the administrator's PC with a dedicated Telegram bot token.

No public IP or inbound port is required. The signing key must never be
shipped to manager installations. See CENTRAL_ACCESS.md before deployment.
"""
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from access_lease import issue

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


def create_server(token, admin_id, key):
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
            registry = load_registry()
            record = registry.get(str(account_id))
            new_request = record is None
            if new_request:
                registry[str(account_id)] = {"subject": subject, "allowed": False}
                save_json(STATE / "users.json", registry)
            allowed = authorized_request(registry, account_id, subject)
            envelope = issue(key, subject=subject, nonce=nonce, allowed=allowed)
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
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Формат: /approve ID или /revoke ID")
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
        registry = load_registry()
        await update.effective_message.reply_text(
            "Центр доступа работает. /approve ID — разрешить; /revoke ID — отключить.\n"
            f"Установок: {len(registry)}, разрешено: "
            f"{sum(r.get('allowed') is True for r in registry.values())}")

    async def users(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != "private":
            return
        registry = load_registry()
        lines = [f"{account}: {'разрешён' if record.get('allowed') is True else 'отключён/ожидает'} — {record.get('subject', '')}"
                 for account, record in registry.items()]
        # Keep responses within Telegram message size limits.
        for start in range(0, max(1, len(lines)), 10):
            await update.effective_message.reply_text("\n".join(lines[start:start+10]) or "Установок пока нет.")

    application.add_handler(CommandHandler("users", users))
    application.add_handler(CommandHandler("lease", lease))
    application.add_handler(CommandHandler("approve", decision))
    application.add_handler(CommandHandler("revoke", decision))
    application.add_handler(CommandHandler("start", status))
    return application


def main():
    # Separate config: never accidentally poll a manager bot with BOT_TOKEN.
    load_dotenv(ROOT / ".env.authority")
    token = os.getenv("ACCESS_AUTHORITY_BOT_TOKEN", "").strip()
    admin_id = int(os.environ["ACCESS_AUTHORITY_ADMIN_ID"])
    if not token or admin_id <= 0:
        raise RuntimeError("Configure .env.authority; see CENTRAL_ACCESS.md")
    key, public = initialize_key()
    print("Public key (safe to distribute): " + public)
    create_server(token, admin_id, key).run_polling()


if __name__ == "__main__":
    main()
