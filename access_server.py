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
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler
from telegram.request import HTTPXRequest

from access_lease import issue,canonical
from authority_store import AuthorityStore
from owner_notifications import manager_label

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data/access_authority"


def register_research_notices(application,store,key,notices):
    async def receive(update,context):
        if store is None or notices is None or update.effective_chat.type!='private':return
        try:
            if len(context.args)!=1 or len(context.args[0])>1500:return
            data=json.loads(base64.b64decode(context.args[0],altchars=b'-_',validate=True))
            if set(data)!={'subject','notice_id','code'}:return
            subject,notice,code=data['subject'],data['notice_id'],data['code']
            account=update.effective_user.id
            if (not isinstance(notice,str) or len(notice)!=64 or any(c not in '0123456789abcdef' for c in notice)
                    or code not in ('quota','credentials')):return
            if not any(r['account_id']==account and r['subject']==subject for r in store.list_managers()):return
            reason=('исчерпан месячный лимит парсера' if code=='quota' else 'парсер отклонил ключ или доступ')
            notices.once(f'research:{account}:{code}:{int(time.time())//3600}',
                f'ДВК Право: {manager_label(store,account)} — {reason}. Подготовка приостановлена. '
                'После восстановления сервиса менеджер может повторить незавершённые исследования; готовые сообщения сохранены.')
            payload=dict(protocol='DVK_RESEARCH_ACK_V1',subject=subject,notice_id=notice)
            envelope=dict(payload=payload,signature=base64.b64encode(key.sign(canonical(payload))).decode())
            await update.effective_message.reply_text('DVK_RESEARCH_ACK '+json.dumps(envelope,separators=(',',':')),disable_notification=True)
        except (ValueError,TypeError,KeyError):return
    application.add_handler(CommandHandler('research_status',receive))


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


def create_server(token, admin_id, key, store=None, sync_key=None, *, role='combined', notices=None, directory=None):
    if role not in ('combined', 'admin', 'service'):
        raise ValueError('Invalid center role')
    application = (Application.builder().token(token)
                   .request(HTTPXRequest(connect_timeout=15, read_timeout=30, write_timeout=30, pool_timeout=10))
                   .get_updates_request(HTTPXRequest(connect_timeout=15, read_timeout=40, write_timeout=30, pool_timeout=10))
                   .build())

    async def lease(update, context):
        if update.effective_chat.type != "private":
            return
        try:
            if len(context.args) != 1 or len(context.args[0]) > 1500:
                raise ValueError("Invalid request")
            request = json.loads(base64.b64decode(context.args[0], altchars=b"-_", validate=True))
            subject, nonce = request["subject"], request["nonce"]
            connection_id = request.get('connection_id')
            account_id = update.effective_user.id
            if (not isinstance(subject, str) or not subject.endswith(f":{account_id}")
                    or len(subject) > 200 or not isinstance(nonce, str)
                    or len(nonce) != 48 or any(c not in "0123456789abcdef" for c in nonce)):
                raise ValueError("Invalid identity or nonce")
            if connection_id is not None and (
                    not isinstance(connection_id, str) or len(connection_id) != 32
                    or any(c not in '0123456789abcdef' for c in connection_id)):
                raise ValueError('Invalid connection identity')
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
            company_id=None
            if store is not None:
                with store.connection() as db:
                    member=db.execute('SELECT company_id FROM managers WHERE account_id=?',(account_id,)).fetchone()
                    company_id=member['company_id'] if member else None
            envelope = issue(key, subject=subject, nonce=nonce, allowed=allowed,company_id=company_id,reporting_schema=1,
                             subscription_end=subscription_end)
            await update.effective_message.reply_text(
                "DVK_ACCESS " + json.dumps(envelope, separators=(",", ":")), disable_notification=True)
            if notices is not None:
                if new_request:
                    notices.once(f'request:{subject}',
                        f'Новый менеджер: Telegram ID {account_id}. Ожидает подключения.\n'
                        f'Разрешить: /approve {account_id} КОМПАНИЯ')
                if allowed:
                    connection_key = f'connected:{subject}' + (f':{connection_id}' if connection_id else '')
                    notices.once(connection_key,
                                 f'Менеджер {manager_label(store, account_id)} подключён. Доступ подтверждён центром.')
            elif new_request:
                await context.bot.send_message(admin_id,
                    f"Новая установка: Telegram ID {account_id}\n"
                    f"Идентификатор: {subject}\n"
                    f"Разрешить: /approve {account_id}" + (" КОМПАНИЯ" if store is not None else "")
                    + f"\nОтключить: /revoke {account_id}")
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
                    if directory is not None and directory.profile(target) is None:
                        prompt = directory.begin(admin_id, target)
                        await update.effective_message.reply_text(
                            'Доступ выдан. Теперь заполним внутреннюю карточку; менеджер этих полей не получит.\n' + prompt)
                        return
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
        from bot_menu import keyboard
        if store is not None:
            await update.effective_message.reply_text(
                'Центр работает.\n/users — менеджеры\n/companies — компании\n'
                '/company ID — создать или включить\n/company_off ID — отключить компанию\n'
                '/renew ID — продлить на месяц + 2 дня\n/approve ID КОМПАНИЯ\n/revoke ID\n'
                'Часовой пояс: Asia/Dubai. dvk — бессрочно.'
                + ('\n/profile ID — внутренняя карточка\n/cancel — отменить заполнение карточки'
                   if directory is not None else ''),reply_markup=keyboard('admin'))
            return
        registry = load_registry()
        await update.effective_message.reply_text(
            "Центр доступа работает. /approve ID — разрешить; /revoke ID — отключить.\n"
            f"Установок: {len(registry)}, разрешено: "
            f"{sum(r.get('allowed') is True for r in registry.values())}",reply_markup=keyboard('admin'))

    async def users(update, context):
        if update.effective_user.id != admin_id or update.effective_chat.type != "private":
            return
        if store is not None:
            lines = [f"{manager_label(store, r['account_id'])}: {r['company_id'] or 'ожидает'}; включён={bool(r['enabled'])}; {r['subject']}"
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

    if role != 'service':
        from service_reports_center import register as register_statistics
        register_statistics(application,store,admin_id)
        from incident_monitor import register_admin
        register_admin(application,store,admin_id)
        for command in ('company', 'company_off', 'renew', 'companies'):
            application.add_handler(CommandHandler(command, companies))
        application.add_handler(CommandHandler("users", users))
        application.add_handler(CommandHandler("approve", decision))
        application.add_handler(CommandHandler("revoke", decision))
        application.add_handler(CommandHandler("start", status))
        if directory is not None:
            from manager_directory import register_admin_handlers
            register_admin_handlers(application, directory, admin_id)
        from bot_menu import register as register_menu
        register_menu(application,role='admin',allowed=lambda u:u.effective_user.id==admin_id and u.effective_chat.type=='private')
    if role != 'admin' and sync_key is not None and store is not None:
        from sync_runtime import register_receiver
        register_receiver(application, store, key, sync_key, notices=notices)
    if role != 'admin':
        application.add_handler(CommandHandler("lease", lease))
        register_research_notices(application,store,key,notices)
        from telegram_safety import register_receiver as register_safety
        register_safety(application, store, key, notices)
    async def on_error(update, context):
        logging.getLogger(__name__).error('Center operation failed (%s)', type(context.error).__name__)
        if notices is not None:
            notices.error('работа центра', context.error)
    application.add_error_handler(on_error)
    return application


def main():
    # Separate config: never accidentally poll a manager bot with BOT_TOKEN.
    load_dotenv(ROOT / ".env.authority")
    token = os.getenv("ACCESS_AUTHORITY_BOT_TOKEN", "").strip()
    service_token = os.getenv('ACCESS_SERVICE_BOT_TOKEN', '').strip()
    service_username = os.getenv('ACCESS_SERVICE_BOT_USERNAME', '').strip().lstrip('@')
    admin_id = int(os.environ["ACCESS_AUTHORITY_ADMIN_ID"])
    if not token or admin_id <= 0:
        raise RuntimeError("Configure .env.authority; see CENTRAL_ACCESS.md")
    if not service_token or not service_username:
        raise RuntimeError('Сначала настройте технического бота: center.bat service-config')
    if token == service_token:
        raise RuntimeError('Нужен отдельный токен технического бота.')
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
        from owner_notifications import OwnerNotifications
        from center_runtime import run_center
        notices = OwnerNotifications(store)
        from manager_directory import ManagerDirectory
        directory = ManagerDirectory(store)
        admin = create_server(token, admin_id, key, store, role='admin', notices=notices, directory=directory)
        service = create_server(service_token, admin_id, key, store, sync_key, role='service', notices=notices)
        app_profiles=None
        if (STATE/'telegram-app-profile.private.json').exists():
            from app_profile_server import AppProfileService,register_handlers
            app_profiles=AppProfileService(store,key,STATE,notices)
            register_handlers(service,app_profiles)
        from dialogue_center import load_broker
        try:
            broker,knowledge_clients=load_broker(STATE,store,key,sync_key)
        except Exception as exc:
            logging.getLogger(__name__).warning('Knowledge configuration unavailable (%s)',type(exc).__name__)
            notices.error('база знаний',exc)
            broker,knowledge_clients=None,[]
        if broker is not None:
            from dialogue_knowledge_transport import register_receiver as register_knowledge
            register_knowledge(service,broker)
        try:
            asyncio.run(run_center(admin, service, notices, admin_id, STATE, service_username,
                                    directory=directory,knowledge_clients=knowledge_clients,app_profiles=app_profiles))
        except KeyboardInterrupt:
            print('Центр остановлен.')


if __name__ == "__main__":
    main()
