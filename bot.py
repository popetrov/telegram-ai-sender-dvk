from access_lease import AccessDenied
import access_runtime
import asyncio
import json
import math
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from telethon.errors import FloodWaitError, PeerFloodError
from ai import (
    extract_message_from_markers,
    generate_company_analysis,
)
from database import (
    get_existing_client_keys,
    get_pending_prepared_messages,
    get_prepared_message,
    init_database,
    normalize_inn,
    normalize_username,
    save_import,
    save_prepared_message,
    save_processed_client,
    start_peerflood_retry,
    start_floodwait_retry,
    update_client_status,
    update_message_status,
    update_prepared_message_status,
)
from telegram_client import client as telegram_client
from dialogue_store import require_contact_allowed, ContactOptOutError

_sender_name_cache = None
_sender_name_lock = asyncio.Lock()


async def legal_sender_name():
    global _sender_name_cache
    async with _sender_name_lock:
        if _sender_name_cache and time.monotonic()-_sender_name_cache[0] < 60:
            return _sender_name_cache[1]
        from ai import profile_first_name
        me = await telegram_client.get_me()
        name = profile_first_name(me.first_name)
        _sender_name_cache = (time.monotonic(),name)
        return name


ALLOWED_USERS_FILE = Path("data/allowed_users.json")
ACCESS_REQUESTS_FILE = Path("data/access_requests.json")
DATA_DIR = Path("data")

MAX_EXCEL_SIZE_BYTES = 20 * 1024 * 1024
MAX_ROWS_PER_IMPORT = 500
MAX_PARALLEL_RESEARCH = 1
OWNER_RESEARCH_ACCOUNT_ID = 925600480
SEND_INTERVAL_SECONDS = 10


def parallel_research_limit():
    # Use the connected Telegram identity, never a manager number or bot-chat ID.
    if type(access_runtime.account_id) is int and access_runtime.account_id == OWNER_RESEARCH_ACCOUNT_ID:
        return 10
    return MAX_PARALLEL_RESEARCH

REQUIRED_COLUMNS = ["Юзернейм", "Продукт"]

QUEUE_KEY = "prepared_send_queue"
PREPARING_KEY = "preparing_import"
SCHEDULE_TASK_KEY = "scheduled_send_task"
delivery_gate = None  # Set by the dialogue runtime only when explicitly enabled.


class TelegramSendPause(RuntimeError):
    """Stop the whole queue; never turn a rate limit into a per-client loop."""


class TelegramSpamPause(TelegramSendPause):
    """Account restriction has no known expiry; only an explicit owner reset clears it."""


_spam_stops_in_memory = set()


def pause_for_spam():
    path = DATA_DIR / 'send_spam_block.json'
    _spam_stops_in_memory.add(path.resolve())
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'blocked': True, 'detected_at': time.time()}), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        return TelegramSpamPause('Telegram ограничил аккаунт (PeerFloodError). Не удалось сохранить блокировку; нужна проверка владельца.')
    return TelegramSpamPause('Telegram ограничил аккаунт (PeerFloodError). Срок не указан. Проверьте @SpamBot; перезапуск не снимает локальную блокировку отправки.')


def require_send_cooldown_finished():
    spam = DATA_DIR / 'send_spam_block.json'
    if spam.exists() or spam.resolve() in _spam_stops_in_memory:
        raise TelegramSpamPause('Отправка заблокирована после PeerFloodError. Сначала проверьте ограничение через @SpamBot. После проверки используйте /spam_reset.')
    path = DATA_DIR / 'send_cooldown.json'
    if path.exists():
        try:
            until = float(json.loads(path.read_text('utf-8'))['until'])
            if not math.isfinite(until):
                raise ValueError('Invalid cooldown')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise TelegramSendPause('Не удалось проверить сохранённую паузу Telegram. Нужна проверка владельца.') from exc
        if until > time.time():
            remaining = math.ceil((until - time.time()) / 60)
            raise TelegramSendPause(f'Telegram требует паузу: ещё примерно {remaining} мин.')


def pause_for_flood(exc):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / 'send_cooldown.json'
        until = time.time() + max(1, int(exc.seconds))
        if path.exists():
            until = max(until, float(json.loads(path.read_text('utf-8'))['until']))
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'until': until}), encoding='utf-8')
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError, KeyError):
        return TelegramSendPause('Telegram потребовал паузу. Не удалось сохранить её срок; нужна проверка владельца.')
    return TelegramSendPause(f'Telegram потребовал паузу примерно на {math.ceil(exc.seconds / 60)} мин.')


def load_allowed_users() -> set[int]:
    if not ALLOWED_USERS_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ALLOWED_USERS_FILE.write_text(
            json.dumps({"allowed_users": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    try:
        data = json.loads(
            ALLOWED_USERS_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Не удалось прочитать data/allowed_users.json"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "Формат allowed_users.json должен содержать объект "
            "с полем allowed_users."
        )

    raw_users = data.get("allowed_users")

    if not isinstance(raw_users, list):
        raise RuntimeError(
            "В allowed_users.json поле allowed_users "
            "должно быть списком Telegram user ID."
        )

    allowed_users = set()

    for value in raw_users:
        if isinstance(value, bool):
            raise RuntimeError(
                "В allowed_users.json обнаружен некорректный ID."
            )

        try:
            user_id = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "В allowed_users.json обнаружен некорректный ID."
            ) from exc

        if user_id <= 0:
            raise RuntimeError(
                "Telegram user ID должен быть положительным числом."
            )

        allowed_users.add(user_id)

    return allowed_users


def save_allowed_users(user_ids: set[int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = ALLOWED_USERS_FILE.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(
            {"allowed_users": sorted(user_ids)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temp_path.replace(ALLOWED_USERS_FILE)


def load_access_requests() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not ACCESS_REQUESTS_FILE.exists():
        return {}

    try:
        data = json.loads(
            ACCESS_REQUESTS_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Не удалось прочитать data/access_requests.json"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "Формат data/access_requests.json должен быть объектом."
        )

    return data


def save_access_requests(requests: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = ACCESS_REQUESTS_FILE.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(requests, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(ACCESS_REQUESTS_FILE)


def get_admin_telegram_id() -> int | None:
    # Fall back to the environment for existing installations whose
    # config.py predates ADMIN_TELEGRAM_ID.
    value = str(
        getattr(config, "ADMIN_TELEGRAM_ID", "")
        or os.getenv("ADMIN_TELEGRAM_ID", "")
        or ""
    ).strip()

    try:
        admin_id = int(value)
    except (TypeError, ValueError):
        return None

    return admin_id if admin_id > 0 else None


def is_controller(update: Update) -> bool:
    if update.effective_user is None:
        return False

    if access_runtime.account_id is not None:
        if (update.effective_user.id != access_runtime.account_id
                or update.effective_chat.type != 'private'):
            return False
    elif update.effective_user.id not in load_allowed_users():
        return False
    return True


def user_is_allowed(update: Update) -> bool:
    if not is_controller(update):
        return False
    try:
        access_runtime.require_access()
        return True
    except AccessDenied:
        return False


def onboarding_text(user_id: int) -> str:
    if access_runtime.account_id is not None:
        if user_id != access_runtime.account_id:
            return 'Этот личный бот доступен только Telegram-аккаунту, подключённому к программе.'
        return ('Ожидается разрешение центрального бота @asdvk2026_bot. '
                'Заявка отправляется программой автоматически. Владелец центра назначает компанию и доступ.\n'
                f'Ваш Telegram ID: {user_id}\n/access — состояние центрального доступа.')
    return (
        "Добро пожаловать в Telegram AI Sender — бот пока не подключён "
        "к разрешённым пользователям.\n\n"
        "Как подключиться:\n"
        "1. Убедитесь, что программа установлена и файл .env заполнен "
        "вашими ключами.\n"
        "2. Отправьте команду /request_access — она отправит заявку "
        "администратору.\n"
        "3. Дождитесь личного уведомления о решении. До одобрения "
        "обработка Excel и отправка сообщений недоступны.\n\n"
        f"Ваш Telegram ID: {user_id}\n\n"
        "После одобрения отправьте Excel .xlsx с колонками "
        "Юзернейм, Продукт и поля выбранного продукта (для текущих ДВК: ИНН, ФИО). Затем используйте /confirm, "
        "чтобы подготовить сообщения. Бот покажет очередь и предложит "
        "время отправки; отправка начинается только после отдельного "
        "подтверждения. /queue — восстановить очередь, /cancel — "
        "отменить запланированную отправку.\n\n"
        "Если заявку не удаётся отправить, сообщите администратору "
        "имя этого бота и попросите один раз открыть его и нажать Start."
    )


async def request_access_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if access_runtime.account_id is not None:
        await update.effective_message.reply_text(onboarding_text(update.effective_user.id))
        return
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message is None or user is None or chat is None:
        return

    if chat.type != "private":
        await message.reply_text(
            "Подать заявку можно только в личном чате с этим ботом."
        )
        return

    if user.id in load_allowed_users():
        await message.reply_text(
            "Доступ уже разрешён. Отправьте Excel-файл .xlsx, "
            "чтобы начать работу."
        )
        return

    admin_id = get_admin_telegram_id()
    if admin_id is None:
        await message.reply_text(
            "Заявка пока не настроена: администратор не указал "
            "ADMIN_TELEGRAM_ID в конфигурации этого бота. "
            "Передайте это сообщение администратору."
        )
        return

    request_id = secrets.token_hex(6)
    username = f"@{user.username}" if user.username else "не указан"
    display_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ) or "Имя не указано"

    requests = load_access_requests()
    existing_request = next(
        (
            request
            for request in requests.values()
            if isinstance(request, dict)
            and request.get("status") == "pending"
            and request.get("user_id") == user.id
        ),
        None,
    )
    if existing_request:
        await message.reply_text(
            "Ваша заявка уже отправлена и ожидает решения администратора. "
            "Повторно отправлять её не нужно."
        )
        return

    requests[request_id] = {
        "user_id": user.id,
        "username": user.username or "",
        "display_name": display_name,
        "chat_id": chat.id,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "pending",
    }
    save_access_requests(requests)

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "Разрешить",
                callback_data=f"access_approve_{request_id}",
            ),
            InlineKeyboardButton(
                "Отклонить",
                callback_data=f"access_reject_{request_id}",
            ),
        ]]
    )

    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=(
                "Новая заявка на подключение Telegram AI Sender.\n\n"
                f"Имя: {display_name}\n"
                f"Username: {username}\n"
                f"Telegram ID: {user.id}\n"
                f"Заявка: {request_id}\n\n"
                "Разрешить доступ этому Telegram-пользователю?"
            ),
            reply_markup=keyboard,
        )
    except Exception as exc:
        requests.pop(request_id, None)
        save_access_requests(requests)
        await message.reply_text(
            "Не удалось доставить заявку администратору. "
            "Попросите его открыть личный чат с этим ботом и нажать "
            "Start, затем отправьте /request_access ещё раз.\n"
            f"Техническая причина: {type(exc).__name__}."
        )
        return

    await message.reply_text(
        "Заявка отправлена администратору. Доступ откроется только "
        "после его подтверждения. Я сообщу вам результат здесь."
    )


async def access_decision_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    admin_id = get_admin_telegram_id()
    if (
        admin_id is None
        or update.effective_user is None
        or update.effective_user.id != admin_id
        or update.effective_chat is None
        or update.effective_chat.type != "private"
    ):
        await query.answer(
            "Только администратор может принимать решение.",
            show_alert=True,
        )
        return

    parts = (query.data or "").split("_")
    if len(parts) != 3 or parts[0] != "access":
        await query.answer("Некорректная заявка.", show_alert=True)
        return

    decision, request_id = parts[1], parts[2]
    if decision not in {"approve", "reject"}:
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    requests = load_access_requests()
    request = requests.get(request_id)
    if not isinstance(request, dict) or request.get("status") != "pending":
        await query.answer(
            "Заявка уже обработана или не найдена.",
            show_alert=True,
        )
        return

    try:
        requester_id = int(request["user_id"])
    except (KeyError, TypeError, ValueError):
        await query.answer(
            "В заявке некорректный Telegram ID.",
            show_alert=True,
        )
        return

    if decision == "approve":
        allowed_users = load_allowed_users()
        allowed_users.add(requester_id)
        save_allowed_users(allowed_users)
        result_text = (
            "Доступ разрешён. Теперь отправьте /start в боте "
            "Telegram AI Sender, чтобы начать работу."
        )
        admin_result = "Доступ разрешён."
        request["status"] = "approved"
    else:
        result_text = (
            "Администратор отклонил заявку на подключение. "
            "Если это ошибка, свяжитесь с администратором."
        )
        admin_result = "Заявка отклонена. Доступ не изменён."
        request["status"] = "rejected"

    request["decided_at"] = datetime.now().astimezone().isoformat()
    request["decided_by"] = admin_id
    save_access_requests(requests)

    await query.answer(admin_result)
    await query.edit_message_text(
        (query.message.text or "Заявка на подключение") + "\n\n" + admin_result
    )

    try:
        await context.bot.send_message(
            chat_id=requester_id,
            text=result_text,
        )
    except Exception:
        pass


def clean_value(value) -> str:
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    if isinstance(value, float) and math.isfinite(value):
        if value.is_integer():
            return str(int(value))

    return str(value).strip()


def normalize_excel_row(row: dict, catalog=None) -> dict:
    from ai import get_catalog
    result = {
        "inn": "",
        "full_name": "",
        "username": normalize_username(clean_value(row.get("Юзернейм"))),
        "product_input": clean_value(row.get("Продукт")),
        "fields": {},
    }
    try:
        settings = (catalog or get_catalog()).settings(result["product_input"])
    except (ValueError, OSError):
        # Report an unknown product through normal per-row validation.
        return result
    fields = {key: clean_value(row.get(column)) for key, column in settings["columns"].items()}
    if "inn" in fields:
        fields["inn"] = normalize_inn(fields["inn"])
    result["fields"] = fields
    result["inn"] = fields.get("inn", "")
    result["full_name"] = fields.get("full_name", "")
    return result


def classify_excel_rows(df: pd.DataFrame, catalog=None) -> tuple[list, list]:
    from ai import get_catalog
    catalog = catalog or get_catalog()
    existing_inns, existing_usernames = get_existing_client_keys()

    seen_inns = set(existing_inns)
    seen_usernames = set(existing_usernames)

    accepted = []
    duplicates = []

    for index, raw_row in df.iterrows():
        row = normalize_excel_row(raw_row.to_dict(), catalog)
        row["excel_row"] = int(index) + 2

        inn = row["inn"]
        username = row["username"]

        if not username:
            row["skip_reason"] = "Не заполнен Юзернейм."
            duplicates.append(row)
            continue

        if (inn and inn in seen_inns) or username in seen_usernames:
            row["skip_reason"] = (
                "ИНН или username уже есть в локальной базе "
                "либо повторяется в этом Excel."
            )
            duplicates.append(row)
            continue

        seen_inns.add(inn)
        seen_usernames.add(username)
        accepted.append(row)

    return accepted, duplicates


def validate_client_row(row: dict, catalog=None) -> str | None:
    from ai import get_catalog
    if not row["username"]:
        return "Не указан username."
    if not row["product_input"]:
        return "Не указан продукт."
    try:
        catalog = catalog or get_catalog()
        product = catalog.normalize(row["product_input"])
        fields = row.get("fields", {"inn": row["inn"], "full_name": row["full_name"]})
        catalog.validate_fields(product, fields)
        # Retain legacy INN validation only when the package uses this field.
        if "inn" in catalog.settings(product)["columns"] and row["inn"] and len(row["inn"]) not in (10, 12):
            return "ИНН должен содержать 10 или 12 цифр."
        row["product"] = product
    except (ValueError, OSError) as exc:
        return f"Ошибка настройки продукта или данных: {exc}"
    return None


def queue_is_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    queue = context.application.bot_data.get(QUEUE_KEY)

    return bool(
        queue
        and queue.get("state") in {
            "preparing",
            "awaiting_time",
            "awaiting_confirmation",
            "scheduled",
            "sending",
        }
    )


def row_identity(item: dict) -> tuple[str, str, str]:
    return (
        normalize_inn(str(item.get("inn") or "")),
        normalize_username(str(item.get("username") or "")),
        str(item.get("product") or "").strip().casefold(),
    )


def is_peerflood_retry(item: dict) -> bool:
    return (
        str(item.get("prepared_status") or "").lower() == "uncertain"
        and str(item.get("send_error") or "").startswith(
            "PeerFloodError:"
        )
    )


def is_floodwait_retry(item: dict) -> bool:
    error=str(item.get('send_error') or '')
    return (item.get('prepared_status')=='manual_review' and error.startswith('FloodWaitError:')
            and 'ResolveUsernameRequest' in error and 'Сообщение не отправлялось.' in error)


def make_prepared_item(
    row: dict,
    analysis: dict,
    final_message: str,
) -> dict:
    return {
        "inn": row["inn"],
        "full_name": row["full_name"],
        "username": row["username"],
        "product": row["product"],
        "prompt_file": str(analysis.get("prompt_file") or ""),
        "prompt_text": str(analysis.get("prompt_text") or ""),
        "model": str(analysis.get("model") or ""),
        "raw_result": str(analysis.get("raw_result") or ""),
        "final_message": final_message,
    }


def item_from_saved_record(record: dict) -> dict:
    item = dict(record)

    prepared_id = (
        item.get("prepared_message_id")
        or item.get("id")
    )

    if not prepared_id:
        raise RuntimeError(
            "У сохранённого сообщения отсутствует ID SQLite."
        )

    required_fields = (
        "inn",
        "full_name",
        "username",
        "product",
        "prompt_file",
        "prompt_text",
        "model",
        "raw_result",
        "final_message",
    )

    missing = [
        field
        for field in required_fields
        if item.get(field) is None
    ]

    if missing:
        raise RuntimeError(
            "В сохранённой записи отсутствуют поля: "
            + ", ".join(missing)
        )

    if not str(item["final_message"]).strip():
        raise RuntimeError(
            "В сохранённой записи пустое готовое сообщение."
        )

    item["prepared_message_id"] = int(prepared_id)
    item["prepared_status"] = str(
        item.get("status") or "prepared"
    ).strip().lower()

    return item


def persist_prepared_item(item: dict) -> dict:
    prepared_id = save_prepared_message(
        inn=item["inn"],
        full_name=item["full_name"],
        username=item["username"],
        product=item["product"],
        prompt_file=item["prompt_file"],
        prompt_text=item["prompt_text"],
        model=item["model"],
        raw_result=item["raw_result"],
        final_message=item["final_message"],
    )

    if isinstance(prepared_id, dict):
        prepared_id = (
            prepared_id.get("id")
            or prepared_id.get("prepared_message_id")
        )

    if not prepared_id:
        raise RuntimeError(
            "save_prepared_message не вернула ID записи SQLite. "
            "Сообщение не будет отправлено."
        )

    item["prepared_message_id"] = int(prepared_id)
    item["prepared_status"] = "prepared"

    return item


def update_prepared_status(
    item: dict,
    status: str,
    send_error: str | None = None,
) -> None:
    prepared_id = item.get("prepared_message_id")

    if not prepared_id:
        raise RuntimeError(
            "У подготовленного сообщения отсутствует ID SQLite."
        )

    update_prepared_message_status(
        prepared_message_id=int(prepared_id),
        status=status,
        send_error=send_error,
    )

    item["prepared_status"] = status

    if send_error is not None:
        item["send_error"] = send_error
    elif status == "sent":
        item["send_error"] = None


async def access_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_controller(update):
        await update.effective_message.reply_text("Сначала запросите локальный доступ: /request_access.")
        return
    try:
        lease = access_runtime.require_access()
        from datetime import timezone
        expires = datetime.fromtimestamp(lease["expires_at"], timezone.utc)
        text = f"Центральный доступ разрешён до {expires:%d.%m.%Y %H:%M} UTC. Проверка каждые 5 минут."
    except AccessDenied as exc:
        text = "Подготовка и отправка приостановлены: " + str(exc) + " Проверка центра повторяется автоматически."
    await update.effective_message.reply_text(text)


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if not user_is_allowed(update):
        await message.reply_text(onboarding_text(user.id))
        return

    from bot_menu import keyboard
    await message.reply_text(
        "Telegram AI Sender готов.\n\n"
        "Отправьте Excel-файл .xlsx с колонками:\n"
        "Юзернейм, Продукт и поля выбранного продукта (для текущих ДВК: ИНН, ФИО).\n\n"
        "После проверки списка используйте /confirm.\n"
        "Готовые сообщения сохраняются в SQLite до отправки.\n"
        "После перезапуска их можно восстановить командой /queue.\n\n"
        "Для отмены запланированной отправки используйте /cancel.",
        reply_markup=keyboard('sender'),
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user_is_allowed(update):
        await update.effective_message.reply_text(
            "Нет доступа. Обратитесь к администратору "
            "в Telegram: @popetrov"
        )
        return

    application = context.application
    queue = application.bot_data.get(QUEUE_KEY)

    if queue and queue.get("state") in {"preparing", "sending"}:
        await update.effective_message.reply_text(
            "Подготовка или отправка уже выполняется. "
            "Отмена на этом этапе не выполняется, чтобы не оставить "
            "очередь в неопределённом состоянии."
        )
        return

    task = application.bot_data.pop(SCHEDULE_TASK_KEY, None)

    if task and not task.done():
        task.cancel()

    application.bot_data.pop(QUEUE_KEY, None)

    context.user_data.pop("pending_rows", None)
    context.user_data.pop("pending_file_name", None)
    context.user_data.pop("pending_duplicate_count", None)

    from schedule_state import clear
    clear()

    await update.effective_message.reply_text(
        "Очередь отменена. Сохранённые подготовленные сообщения "
        "остаются в SQLite. Для восстановления используйте /queue."
    )


async def handle_excel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user_is_allowed(update):
        await update.effective_message.reply_text(
            "Нет доступа. Обратитесь к администратору "
            "в Telegram: @popetrov"
        )
        return

    if queue_is_active(context):
        await update.effective_message.reply_text(
            "Сейчас уже существует очередь подготовки или отправки. "
            "Сначала завершите её либо отмените командой /cancel. "
            "Новый Excel пока не принят."
        )
        return

    if context.application.bot_data.get(PREPARING_KEY):
        await update.effective_message.reply_text(
            "Подготовка предыдущего импорта уже выполняется. "
            "Дождитесь её завершения."
        )
        return

    document = update.effective_message.document

    if document is None:
        await update.effective_message.reply_text(
            "Пришлите файл Excel формата .xlsx."
        )
        return

    if (
        not document.file_name
        or not document.file_name.lower().endswith(".xlsx")
    ):
        await update.effective_message.reply_text(
            "Поддерживается только файл .xlsx."
        )
        return

    if (
        document.file_size is not None
        and document.file_size > MAX_EXCEL_SIZE_BYTES
    ):
        await update.effective_message.reply_text(
            "Файл превышает допустимый размер 20 МБ."
        )
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    user_id = update.effective_user.id
    temp_path = DATA_DIR / f"pending_{user_id}.xlsx"

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(
            custom_path=str(temp_path)
        )

        try:
            df = pd.read_excel(
                temp_path,
                dtype=str,
                engine="openpyxl",
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"Не удалось прочитать Excel-файл: {exc}"
            )
            return

        missing_columns = [
            column
            for column in REQUIRED_COLUMNS
            if column not in df.columns
        ]

        if missing_columns:
            await update.effective_message.reply_text(
                "В файле отсутствуют обязательные колонки: "
                + ", ".join(missing_columns)
            )
            return

        if len(df) > MAX_ROWS_PER_IMPORT:
            await update.effective_message.reply_text(
                f"В файле {len(df)} строк. "
                f"За один импорт разрешено не более "
                f"{MAX_ROWS_PER_IMPORT}."
            )
            return

        from ai import get_catalog
        try:
            catalog = get_catalog()
        except (ValueError, OSError) as exc:
            await update.effective_message.reply_text("Ошибка бизнес-пакета: " + str(exc))
            return
        # Save the uploaded client fields before classification, including duplicates.
        # Ignore unrelated spreadsheet columns; never upload settings or credentials.
        import json
        from database import get_connection
        with get_connection() as db:
            business_columns = set(REQUIRED_COLUMNS)
            for settings in catalog.products.values():
                business_columns.update(settings['columns'].values())
            columns = [name for name in df.columns if name in business_columns]
            for record in df[columns].fillna('').to_dict(orient='records'):
                db.execute('INSERT INTO uploaded_rows(data) VALUES (?)',
                           (json.dumps(record, ensure_ascii=False),))
        accepted_rows, duplicate_rows = classify_excel_rows(df, catalog)

        valid_rows = []
        invalid_rows = []

        for row in accepted_rows:
            validation_error = validate_client_row(row, catalog)

            if validation_error:
                row["skip_reason"] = validation_error
                invalid_rows.append(row)
            else:
                valid_rows.append(row)

        if not valid_rows:
            await update.effective_message.reply_text(
                "Нет новых строк, готовых к обработке.\n"
                f"Всего строк: {len(df)}\n"
                f"Дубли: {len(duplicate_rows)}\n"
                f"Ошибки заполнения: {len(invalid_rows)}"
            )
            return

        context.user_data["pending_rows"] = valid_rows
        context.user_data["pending_file_name"] = document.file_name
        context.user_data["pending_duplicate_count"] = (
            len(duplicate_rows) + len(invalid_rows)
        )

        preview_lines = [
            "Excel проверен.",
            "",
            f"Всего строк: {len(df)}",
            f"Дубли: {len(duplicate_rows)}",
            f"Ошибки заполнения: {len(invalid_rows)}",
            f"Новых клиентов к обработке: {len(valid_rows)}",
            "",
            "Клиенты:",
        ]

        for index, row in enumerate(valid_rows[:30], start=1):
            preview_lines.append(
                f"{index}. {row['inn']} | {row['full_name']} | "
                f"@{row['username']} | {row['product']}"
            )

        if len(valid_rows) > 30:
            preview_lines.append(
                f"... и ещё {len(valid_rows) - 30} клиентов."
            )

        preview_lines.extend(
            [
                "",
                "Команда /confirm подготовит сообщения для клиентов.",
                "Ранее сохранённые сообщения будут использованы повторно.",
                "Команда /cancel отменит очередь.",
            ]
        )

        await update.effective_message.reply_text(
            "\n".join(preview_lines)
        )

    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


async def prepare_one_client(
    row: dict,
) -> tuple[str, str, dict | None]:
    try:
        access_runtime.require_access()
    except AccessDenied as exc:
        return "manual_review", str(exc), None
    inn = row["inn"]
    full_name = row["full_name"]
    username = row["username"]
    product = row["product"]

    try:
        saved_record = get_prepared_message(
            inn=inn,
            username=username,
            product=product,
        )
    except Exception as exc:
        return (
            "persistence_failed",
            "Не удалось проверить очередь SQLite. "
            "GPT не вызывается.\n"
            f"{type(exc).__name__}: {exc}",
            None,
        )

    if saved_record:
        saved_status = str(
            saved_record.get("status") or "prepared"
        ).strip().lower()

        send_error = str(
            saved_record.get("send_error") or ""
        )

        is_explicit_peer_flood = (
            saved_status == "uncertain"
            and send_error.startswith("PeerFloodError:")
        )

        if saved_status == "prepared" or is_explicit_peer_flood:
            try:
                item = item_from_saved_record(saved_record)
            except Exception as exc:
                return (
                    "manual_review",
                    "Найдено сохранённое сообщение, но восстановить "
                    "его безопасно не удалось. GPT не вызывается.\n"
                    f"{type(exc).__name__}: {exc}",
                    None,
                )

            if is_explicit_peer_flood:
                item["prepared_status"] = "uncertain"
                item["send_error"] = send_error

                return (
                    "prepared",
                    "Восстановлено сохранённое сообщение с "
                    "PeerFloodError. Исходный текст не изменён, "
                    "повторное исследование не выполнялось.",
                    item,
                )

            return (
                "prepared",
                "Восстановлено ранее подготовленное сообщение из SQLite. "
                "GPT повторно не вызывался.",
                item,
            )

        return (
            "manual_review",
            "Для клиента найдена сохранённая запись со статусом "
            f"«{saved_status}». Повторная подготовка запрещена. "
            "Проверьте соответствующий Telegram-чат вручную.",
            None,
        )

    try:
        existing_inns, existing_usernames = get_existing_client_keys()
    except Exception as exc:
        return (
            "persistence_failed",
            "Не удалось проверить основную базу SQLite. "
            "GPT не вызывается.\n"
            f"{type(exc).__name__}: {exc}",
            None,
        )

    if inn in existing_inns or username in existing_usernames:
        return (
            "duplicate",
            "Клиент уже есть в основной локальной базе. "
            "Подготовка пропущена.",
            None,
        )

    try:
        sender_fields = {}
        from ai import normalize_product
        if normalize_product(product) in {'Право_ООО','Право_ИП'}:
            sender_fields['sender_name'] = await legal_sender_name()
        analysis = await asyncio.to_thread(
            generate_company_analysis,
            inn,
            full_name,
            username,
            product,
            fields=row.get("fields"),
            **sender_fields,
        )
    except Exception as exc:
        from legal_research import ResearchProblem
        if isinstance(exc,ResearchProblem):
            return (exc.status,str(exc),None)
        return (
            "research_failed",
            f"Ошибка исследования: {type(exc).__name__}: {exc}",
            None,
        )

    raw_result = str(analysis.get("raw_result") or "")

    if not raw_result.strip():
        return (
            "research_failed",
            "ИИ вернул пустой результат. Сообщение не сохранено.",
            None,
        )

    if "ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА" in raw_result.upper():
        return (
            "manual_review",
            "ИИ отметил результат как требующий ручной проверки. "
            "Автоматическая отправка запрещена.",
            None,
        )

    try:
        final_message = extract_message_from_markers(raw_result)
    except Exception as exc:
        return (
            "manual_review",
            "Не удалось безопасно извлечь текст сообщения. "
            "Автоматическая отправка запрещена. "
            f"Причина: {type(exc).__name__}: {exc}",
            None,
        )

    if not final_message or not final_message.strip():
        return (
            "manual_review",
            "Подготовленное сообщение пустое. Отправка запрещена.",
            None,
        )

    item = make_prepared_item(row, analysis, final_message)

    try:
        item = persist_prepared_item(item)
    except Exception as exc:
        return (
            "persistence_failed",
            "GPT подготовил сообщение, но сохранить его в SQLite "
            "не удалось. Оно не будет отправлено.\n"
            f"{type(exc).__name__}: {exc}",
            None,
        )

    if analysis.get('legal_job_key'):
        from legal_research import Journal
        try:Journal(config.MANAGER_ID).ready(analysis['legal_job_key'])
        except Exception:
            # The prepared row is durable; a later retry restores it before any new research.
            pass
    return (
        "prepared",
        ("Сообщение подготовлено по проверенным судебным реквизитам и сохранено в SQLite."
         + (" Использована стандартная формулировка по проверенным фактам." if analysis.get('legal_fallback') else '')
         if analysis.get('legal_job_key') else "Сообщение подготовлено и сохранено в SQLite."),
        item,
    )


async def retry_research_command(update,context):
    if not user_is_allowed(update):
        await update.effective_message.reply_text('Нет доступа. Проверьте /access.');return
    old=context.application.bot_data.get(QUEUE_KEY)
    if context.application.bot_data.get(PREPARING_KEY) or (old and old.get('state') in {'preparing','scheduled','sending'}):
        await update.effective_message.reply_text('Подготовка или запланированная отправка уже выполняется. Сначала завершите её; очередь не изменена.');return
    if context.user_data.get('pending_rows'):
        await update.effective_message.reply_text('Уже загружен новый Excel. Подтвердите или отмените его перед повтором исследований.');return
    from legal_research import Journal
    from arbitration_api import reset_service_block
    rows=Journal(config.MANAGER_ID).retry_rows()
    if not rows:
        await update.effective_message.reply_text('Незавершённых исследований ДВК Право нет.');return
    reset_service_block(config.PARSER_API_KEY)
    context.user_data['pending_rows']=rows
    context.user_data['pending_file_name']='Повтор незавершённых исследований ДВК Право'
    context.user_data['_research_kept_items']=list(old.get('items',[])) if old and old.get('state') in {'awaiting_time','awaiting_confirmation'} else []
    context.application.bot_data.pop(QUEUE_KEY,None)
    try:await confirm_command(update,context)
    finally:
        if QUEUE_KEY not in context.application.bot_data and old:context.application.bot_data[QUEUE_KEY]=old
        context.user_data.pop('_research_kept_items',None)


async def confirm_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user_is_allowed(update):
        await update.effective_message.reply_text(
            "Нет доступа. Обратитесь к администратору "
            "в Telegram: @popetrov"
        )
        return

    application = context.application

    if application.bot_data.get(PREPARING_KEY):
        await update.effective_message.reply_text(
            "Подготовка уже выполняется. Дождитесь её завершения."
        )
        return

    if queue_is_active(context):
        await update.effective_message.reply_text(
            "Уже существует очередь подготовки или отправки. "
            "Повторная обработка не запущена."
        )
        return

    rows = context.user_data.get("pending_rows")

    if not rows:
        await update.effective_message.reply_text(
            "Нет подготовленного списка компаний. "
            "Сначала загрузи Excel-файл."
        )
        return

    rows = list(rows)

    file_name = context.user_data.get(
        "pending_file_name",
        "unknown.xlsx",
    )
    duplicate_count = context.user_data.get(
        "pending_duplicate_count",
        0,
    )

    queue = {
        "state": "preparing",
        "owner_user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "file_name": file_name,
        "duplicate_count": duplicate_count,
        "items": context.user_data.pop('_research_kept_items',[]),
        "preparation_counts": {
            "prepared": 0,
            "duplicate": 0,
            "research_failed": 0,
            "manual_review": 0,
            "persistence_failed": 0,
        },
        "send_at": None,
    }

    application.bot_data[QUEUE_KEY] = queue
    application.bot_data[PREPARING_KEY] = True

    context.user_data.pop("pending_rows", None)
    context.user_data.pop("pending_file_name", None)
    context.user_data.pop("pending_duplicate_count", None)

    try:
        try:
            save_import(
                file_name=file_name,
                total_rows=len(rows) + duplicate_count,
                duplicate_rows=duplicate_count,
                new_rows=len(rows),
            )
        except Exception as exc:
            context.user_data["pending_rows"] = rows
            context.user_data["pending_file_name"] = file_name
            context.user_data["pending_duplicate_count"] = (
                duplicate_count
            )

            application.bot_data.pop(QUEUE_KEY, None)

            await update.effective_message.reply_text(
                "Не удалось записать импорт в журнал SQLite. "
                "Подготовка остановлена, список восстановлен.\n"
                f"{type(exc).__name__}: {exc}"
            )
            return

        total = len(rows)
        if queue['items']:
            await update.effective_message.reply_text(f"Сохраняю в очереди ранее готовые сообщения: {len(queue['items'])}. Повторно они не исследуются.")
        parallel_limit = parallel_research_limit()
        semaphore = asyncio.Semaphore(parallel_limit)

        await update.effective_message.reply_text(
            f"Начинаю обработку {total} компаний.\n\n"
            f"Одновременных исследований: {parallel_limit}.\n"
            "Сохранённые сообщения будут восстановлены из SQLite.\n"
            "Новые исследования выполняются только для клиентов, "
            "для которых нет сохранённого результата.\n"
            "Автоматической отправки после подготовки не будет."
        )

        async def prepare_with_limit(
            index: int,
            row: dict,
        ) -> tuple[int, dict, str, str, dict | None]:
            async with semaphore:
                try:
                    status, details, prepared_item = (
                        await prepare_one_client(row)
                    )
                except Exception as exc:
                    status = "research_failed"
                    details = (
                        f"Непредвиденная ошибка: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    prepared_item = None

                return (
                    index,
                    row,
                    status,
                    details,
                    prepared_item,
                )

        tasks = [
            asyncio.create_task(
                prepare_with_limit(index, row)
            )
            for index, row in enumerate(rows, start=1)
        ]

        try:
            completed = 0

            for task in asyncio.as_completed(tasks):
                (
                    index,
                    row,
                    status,
                    details,
                    prepared_item,
                ) = await task

                completed += 1

                counts = queue["preparation_counts"]

                if status not in counts:
                    counts[status] = 0

                counts[status] += 1

                if prepared_item is not None:
                    def same_item(v):
                        previous=v.get('prepared_message_id');current=prepared_item.get('prepared_message_id')
                        if previous is not None and current is not None:return previous==current
                        identity=row_identity(prepared_item)
                        return all(identity) and row_identity(v)==identity
                    if not any(same_item(v) for v in queue['items']):
                        queue["items"].append(prepared_item)

                await update.effective_message.reply_text(
                    f"Подготовка {completed}/{total}\n"
                    f"ИНН: {row['inn']}\n"
                    f"Клиент: @{row['username']}\n"
                    f"Статус: {({'research_failed':'Нужно повторить исследование','manual_review':'Нужна проверка данных','research_paused':'Приостановлено: сервис недоступен'}.get(status,status) if row.get('product') in {'Право_ООО','Право_ИП'} else status)}\n"
                    f"{details}"
                )

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        counts = queue["preparation_counts"]
        prepared_count = len(queue["items"])
        from bot_menu import keyboard
        completion_keyboard=keyboard('sender')

        if prepared_count == 0:
            application.bot_data.pop(QUEUE_KEY, None)

            await update.effective_message.reply_text(
                "Подготовка завершена, но нет сообщений, "
                "готовых к отправке.\n\n"
                f"Всего компаний: {total}\n"
                f"Восстановлено/подготовлено: "
                f"{counts.get('prepared', 0)}\n"
                f"Дубли: {counts.get('duplicate', 0)}\n"
                f"Ошибки исследования: "
                f"{counts.get('research_failed', 0)}\n"
                f"Приостановлены из-за сервиса: {counts.get('research_paused', 0)}\n"
                f"Ручная проверка: "
                f"{counts.get('manual_review', 0)}\n"
                f"Ошибки сохранения: "
                f"{counts.get('persistence_failed', 0)}\n\n"
                "Данные SQLite не удалены."
                ,reply_markup=completion_keyboard
            )
            return

        queue["state"] = "awaiting_time"

        await update.effective_message.reply_text(
            "Подготовка завершена.\n\n"
            f"Всего компаний: {total}\n"
            f"Сообщений в очереди: {prepared_count}\n"
            f"Дубли: {counts.get('duplicate', 0)}\n"
            f"Ошибки исследования: "
            f"{counts.get('research_failed', 0)}\n"
            f"Приостановлены из-за сервиса: {counts.get('research_paused', 0)}\n"
            f"Ручная проверка: "
            f"{counts.get('manual_review', 0)}\n"
            f"Ошибки сохранения: "
            f"{counts.get('persistence_failed', 0)}\n\n"
            "Все готовые сообщения сохранены в SQLite.\n"
            "Сообщения пока не отправлены.\n\n"
            "Убедитесь, что ограничение Telegram снято. "
            "Затем введите время в формате ЧЧ:ММ."
            ,reply_markup=completion_keyboard
        )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        queue["state"] = "interrupted"

        await update.effective_message.reply_text(
            "Подготовка прервана из-за ошибки.\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "Уже сохранённые сообщения остаются в SQLite. "
            "Проверьте очередь командой /queue перед повторным запуском."
        )

    finally:
        application.bot_data.pop(PREPARING_KEY, None)


async def send_one_prepared_message(
    item: dict,
) -> tuple[str, str]:
    if delivery_gate is None:
        return await _send_one_prepared_message(item)
    def guard():
        access_runtime.require_access()
        require_send_cooldown_finished()
    return await delivery_gate.run(lambda: _send_one_prepared_message(item),guard)


async def _send_one_prepared_message(
    item: dict,
) -> tuple[str, str]:
    """
    PeerFloodError допускается к единственной контролируемой попытке
    восстановления. Resolver-FloodWait допускается только после проверки
    истории в SQLite. Остальные uncertain-записи не отправляются.
    """

    access_runtime.require_access()
    require_send_cooldown_finished()
    inn = item["inn"]
    full_name = item["full_name"]
    username = item["username"]
    product = item["product"]
    final_message = item["final_message"]

    peerflood_retry = is_peerflood_retry(item)
    floodwait_retry = is_floodwait_retry(item)

    if (
        item.get("prepared_status") != "prepared"
        and not peerflood_retry
        and not floodwait_retry
    ):
        return (
            "manual_review",
            "Запись не имеет статуса prepared или явно подтверждённого "
            "PeerFloodError или проверенного отказа FloodWait до отправки. Отправка запрещена.",
        )

    if peerflood_retry or floodwait_retry:
        try:
            start_retry = start_floodwait_retry if floodwait_retry else start_peerflood_retry
            client_id, research_run_id, message_id = (
                start_retry(
                    prepared_message_id=int(
                        item["prepared_message_id"]
                    ),
                    inn=inn,
                    full_name=full_name,
                    username=username,
                    product=product,
                    prompt_file=item["prompt_file"],
                    prompt_text=item["prompt_text"],
                    model=item["model"],
                    raw_result=item["raw_result"],
                    final_message=final_message,
                )
            )
        except Exception as exc:
            return (
                "persistence_failed",
                "Не удалось безопасно перевести исходную запись "
                "после отказа Telegram в повторную отправку. "
                "Сообщение не отправлялось.\n"
                f"{type(exc).__name__}: {exc}",
            )

        item["prepared_status"] = "sending"
        item["peerflood_retry_started"] = True

    else:
        try:
            existing_inns, existing_usernames = (
                get_existing_client_keys()
            )
        except Exception as exc:
            return (
                "persistence_failed",
                "Не удалось проверить дубли в SQLite. "
                "Сообщение не отправлялось.\n"
                f"{type(exc).__name__}: {exc}",
            )

        if inn in existing_inns or username in existing_usernames:
            try:
                update_prepared_status(item, "duplicate")
            except Exception:
                pass

            return (
                "duplicate",
                "Клиент уже появился в основной локальной базе. "
                "Отправка пропущена.",
            )

        try:
            entity = await telegram_client.get_entity(username)
        except FloodWaitError as exc:
            raise pause_for_flood(exc) from exc
        except PeerFloodError as exc:
            raise pause_for_spam() from exc
        except Exception as exc:
            return (
                "send_failed",
                f"Не удалось найти Telegram-пользователя @{username}: "
                f"{type(exc).__name__}: {exc}. Сообщение не отправлялось.",
            )

        try:
            client_id, research_run_id, message_id = (
                save_processed_client(
                    inn=inn,
                    full_name=full_name,
                    username=username,
                    product=product,
                    prompt_file=item["prompt_file"],
                    prompt_text=item["prompt_text"],
                    model=item["model"],
                    raw_result=item["raw_result"],
                    final_message=final_message,
                    status="sending",
                )
            )
        except Exception as exc:
            return (
                "persistence_failed",
                "Не удалось создать основную запись в SQLite. "
                "Сообщение не отправлялось.\n"
                f"{type(exc).__name__}: {exc}",
            )

        try:
            update_prepared_status(item, "sending")
        except Exception as exc:
            try:
                update_message_status(
                    message_id=message_id,
                    status="not_sent",
                    send_error=(
                        "Статус подготовленной записи не обновлён: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
                update_client_status(client_id, "manual_review")
            except Exception:
                pass

            return (
                "persistence_failed",
                "Не удалось обновить статус подготовленного сообщения. "
                "Отправка отменена.",
            )

    try:
        entity = await telegram_client.get_entity(username)
    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}. "
            "Сообщение не отправлялось."
        )

        try:
            update_message_status(
                message_id=message_id,
                status="not_sent",
                send_error=error_text,
            )
        except Exception:
            pass

        try:
            update_client_status(
                client_id=client_id,
                status="manual_review",
            )
        except Exception:
            pass

        try:
            update_prepared_status(
                item,
                "manual_review",
                error_text,
            )
        except Exception:
            pass

        if isinstance(exc, FloodWaitError):
            raise pause_for_flood(exc) from exc
        if isinstance(exc, PeerFloodError):
            raise pause_for_spam() from exc
        return "send_failed", error_text

    # This runs after peer resolution too: a revocation received while awaiting
    # Telegram must prevent the next outgoing client message.
    try:
        access_runtime.require_access()
        require_contact_allowed(config.DATABASE_PATH,manager_id=config.MANAGER_ID,peer_id=getattr(entity,'id',None))
    except AccessDenied as exc:
        update_message_status(message_id=message_id, status="not_sent", send_error=str(exc))
        update_client_status(client_id, "manual_review")
        update_prepared_status(item, "manual_review", "Доступ отключён до отправки: " + str(exc))
        raise
    except ContactOptOutError as exc:
        update_message_status(message_id=message_id,status='not_sent',send_error=str(exc))
        update_client_status(client_id,'manual_review')
        update_prepared_status(item,'manual_review',str(exc))
        return 'send_failed',str(exc)
    try:
        sent_message = await telegram_client.send_message(
            entity,
            final_message,
        )
    except Exception as exc:
        from telegram_safety import SafetyStop
        if isinstance(exc, SafetyStop):
            # A protection stop during an SDK retry cannot prove an earlier dispatch failed.
            update_message_status(message_id=message_id, status='uncertain', send_error=str(exc))
            update_client_status(client_id, 'manual_review')
            update_prepared_status(item, 'manual_review', str(exc))
            raise
        error_text = f"{type(exc).__name__}: {exc}"

        try:
            update_message_status(
                message_id=message_id,
                status="uncertain",
                send_error=error_text,
            )
        except Exception:
            pass

        try:
            update_client_status(
                client_id=client_id,
                status="uncertain",
                telegram_id=getattr(entity, "id", None),
            )
        except Exception:
            pass

        try:
            update_prepared_status(
                item,
                "uncertain",
                error_text,
            )
        except Exception:
            pass

        if isinstance(exc, FloodWaitError):
            raise pause_for_flood(exc) from exc
        if isinstance(exc, PeerFloodError):
            raise pause_for_spam() from exc
        return (
            "uncertain",
            f"{error_text}. Автоматический повтор запрещён. "
            "Проверьте чат вручную.",
        )

    update_errors = []

    try:
        update_message_status(
            message_id=message_id,
            status="sent",
            telegram_message_id=sent_message.id,
            send_error=None,
        )
    except Exception as exc:
        update_errors.append(
            f"messages: {type(exc).__name__}: {exc}"
        )

    try:
        update_client_status(
            client_id=client_id,
            status="sent",
            telegram_id=getattr(entity, "id", None),
        )
    except Exception as exc:
        update_errors.append(
            f"clients: {type(exc).__name__}: {exc}"
        )

    try:
        update_prepared_status(item, "sent")
    except Exception as exc:
        update_errors.append(
            f"prepared_messages: {type(exc).__name__}: {exc}"
        )

    if update_errors:
        return (
            "sent_db_update_failed",
            "Telegram подтвердил отправку. Не удалось обновить "
            "часть статусов SQLite:\n"
            + "\n".join(update_errors)
            + "\nПовторная отправка запрещена.",
        )

    return (
        "sent",
        f"Сообщение отправлено @{username}; "
        f"Telegram message ID: {sent_message.id}.",
    )


async def spam_reset_command(update, context):
    if not user_is_allowed(update):
        return
    if context.args != ['ПРОВЕРЕНО']:
        await update.effective_message.reply_text(
            'Сначала проверьте аккаунт через @SpamBot. Только после проверки снятия ограничения '
            'отправьте /spam_reset ПРОВЕРЕНО. Это снимет лишь локальную остановку программы, '
            'не ограничение Telegram. Очередь сама не запустится.')
        return
    path = DATA_DIR / 'send_spam_block.json'
    path.unlink(missing_ok=True)
    _spam_stops_in_memory.discard(path.resolve())
    await update.effective_message.reply_text(
        'Локальная остановка после PeerFloodError снята по вашему подтверждению. '
        'Записи и очередь не изменены, отправка не запущена. '
        'Перед восстановлением проверьте результаты; при новом PeerFloodError очередь снова остановится.')


async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user_is_allowed(update):
        await update.effective_message.reply_text(
            "Нет доступа. Обратитесь к администратору "
            "в Telegram: @popetrov"
        )
        return

    if queue_is_active(context):
        await update.effective_message.reply_text(
            "Уже есть активная очередь. Сначала завершите её "
            "или отмените запланированную отправку."
        )
        return

    try:
        rows = get_pending_prepared_messages()
    except Exception as exc:
        await update.effective_message.reply_text(
            "Не удалось прочитать сохранённые сообщения из SQLite.\n"
            f"{type(exc).__name__}: {exc}"
        )
        return

    items = []
    rejected_peerflood = 0

    for row in rows:
        try:
            row_dict = dict(row)
            status = str(
                row_dict.get("status") or "prepared"
            ).strip().lower()

            explicit_peerflood = (
                status == "uncertain"
                and str(
                    row_dict.get("send_error") or ""
                ).startswith("PeerFloodError:")
            )

            explicit_floodwait = is_floodwait_retry({**row_dict, 'prepared_status': status})
            if status != "prepared" and not explicit_peerflood and not explicit_floodwait:
                continue

            item = item_from_saved_record(row_dict)

            if explicit_peerflood:
                item["prepared_status"] = "uncertain"
                item["send_error"] = str(row_dict["send_error"])

            items.append(item)

        except Exception:
            rejected_peerflood += 1
            continue

    if not items:
        await update.effective_message.reply_text(
            "В SQLite нет сообщений, разрешённых к восстановлению.\n"
            "Статусы uncertain без явного PeerFloodError, sent, "
            "sending и непроверенные manual_review не добавляются."
        )
        return

    context.application.bot_data[QUEUE_KEY] = {
        "state": "awaiting_time",
        "owner_user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "file_name": "restored_from_sqlite",
        "duplicate_count": 0,
        "items": items,
        "preparation_counts": {"prepared": len(items)},
        "send_at": None,
    }

    peerflood_count = sum(
        1 for item in items if is_peerflood_retry(item)
    )
    floodwait_count = sum(1 for item in items if is_floodwait_retry(item))

    await update.effective_message.reply_text(
        "Очередь восстановлена из SQLite.\n\n"
        f"Всего сообщений: {len(items)}\n"
        f"Из них с явным PeerFloodError: {peerflood_count}\n"
        f"Точно не отправлены при поиске (FloodWait): {floodwait_count}\n"
        f"Обычных prepared: {len(items) - peerflood_count - floodwait_count}\n"
        "Исходные сохранённые тексты не изменены.\n"
        "GPT повторно не вызывается.\n\n"
        "Перед подтверждением проверьте, что ограничение Telegram "
        "снято. Если ошибка повторится, повторно эти записи "
        "автоматически отправляться не будут.\n\n"
        "Для выбора времени отправки введите ЧЧ:ММ."
    )


async def schedule_time_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user_is_allowed(update):
        return

    queue = context.application.bot_data.get(QUEUE_KEY)

    if not queue or queue.get("state") != "awaiting_time":
        return

    if queue.get("owner_user_id") != update.effective_user.id:
        return

    text = (update.effective_message.text or "").strip()

    try:
        parsed_time = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        await update.effective_message.reply_text(
            "Введите время в формате ЧЧ:ММ, например 18:30."
        )
        return

    now = datetime.now().astimezone()

    send_at = now.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )

    if send_at <= now:
        await update.effective_message.reply_text(
            "Это время уже наступило или прошло. "
            "Введите более позднее время."
        )
        return

    queue["send_at"] = send_at
    queue["state"] = "awaiting_confirmation"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Подтвердить отправку в {send_at:%H:%M}",
                    callback_data="queue_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    "Отменить очередь",
                    callback_data="queue_cancel",
                )
            ],
        ]
    )

    await update.effective_message.reply_text(
        "Проверьте параметры отправки.\n\n"
        f"Готовых сообщений: {len(queue['items'])}\n"
        f"Время: {send_at:%H:%M}\n"
        f"Дата: {send_at:%d.%m.%Y}\n\n"
        "Отправка начнётся только после подтверждения.",
        reply_markup=keyboard,
    )


async def queue_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    if not user_is_allowed(update):
        await query.answer("Нет доступа.", show_alert=True)
        return

    queue = context.application.bot_data.get(QUEUE_KEY)

    if not queue:
        await query.answer(
            "Очередь уже отсутствует.",
            show_alert=True,
        )
        return

    if queue.get("owner_user_id") != update.effective_user.id:
        await query.answer("Это не ваша очередь.", show_alert=True)
        return

    if query.data == "queue_cancel":
        if queue.get("state") not in {
            "awaiting_time",
            "awaiting_confirmation",
            "scheduled",
        }:
            await query.answer(
                "На этом этапе отмена недоступна.",
                show_alert=True,
            )
            return

        task = context.application.bot_data.pop(
            SCHEDULE_TASK_KEY,
            None,
        )

        if task and not task.done():
            task.cancel()

        context.application.bot_data.pop(QUEUE_KEY, None)

        from schedule_state import clear
        clear()
        await query.answer("Очередь отменена.")
        await query.edit_message_text(
            "Очередь отменена. Подготовленные сообщения сохранены "
            "в SQLite. Для восстановления используйте /queue."
        )
        return

    if query.data == "queue_confirm":
        if queue.get("state") != "awaiting_confirmation":
            await query.answer(
                "Эта очередь уже подтверждена или недоступна.",
                show_alert=True,
            )
            return

        send_at = queue.get("send_at")

        if not send_at or send_at <= datetime.now().astimezone():
            queue["state"] = "awaiting_time"
            queue["send_at"] = None

            await query.answer(
                "Назначенное время уже наступило. Укажите новое.",
                show_alert=True,
            )
            await query.edit_message_text(
                "Назначенное время уже наступило. "
                "Введите новое время в формате ЧЧ:ММ."
            )
            return

        from schedule_state import save
        queue["state"] = "scheduled"
        save(queue)

        task = asyncio.create_task(
            send_queue_at_time(
                application=context.application,
                queue=queue,
            )
        )

        context.application.bot_data[SCHEDULE_TASK_KEY] = task

        await query.answer("Отправка запланирована.")
        await query.edit_message_text(
            f"Отправка подтверждена: {send_at:%H:%M} "
            f"({send_at:%d.%m.%Y}).\n\n"
            f"Сообщений: {len(queue['items'])}.\n"
            "Оставьте программу запущенной."
        )


async def send_queue_at_time(
    application: Application,
    queue: dict,
) -> None:
    try:
        send_at = queue["send_at"]

        delay = (
            send_at - datetime.now().astimezone()
        ).total_seconds()

        if delay > 0:
            await asyncio.sleep(delay)

        if application.bot_data.get(QUEUE_KEY) is not queue:
            return

        if queue.get("state") != "scheduled":
            return

        queue["state"] = "sending"

        chat_id = queue["chat_id"]
        items = queue["items"]

        counts = {
            "sent": 0,
            "duplicate": 0,
            "send_failed": 0,
            "uncertain": 0,
            "sent_db_save_failed": 0,
            "sent_db_update_failed": 0,
            "persistence_failed": 0,
            "manual_review": 0,
        }

        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                "Наступило назначенное время. Начинаю отправку.\n"
                f"Сообщений в очереди: {len(items)}."
            ),
        )

        for index, item in enumerate(items, start=1):
            if (
                item.get("prepared_status") != "prepared"
                and not is_peerflood_retry(item)
                and not is_floodwait_retry(item)
            ):
                continue

            try:
                access_runtime.require_access()
                status, details = await send_one_prepared_message(item)
            except TelegramSpamPause as exc:
                queue['state'] = 'interrupted'
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(str(exc) + ' Вся очередь остановлена, следующие получатели не обрабатываются. '
                          'Текущий результат требует проверки; автоматического повтора нет.\n\n'
                          f"Итог этого запуска: всего {len(items)}.\n"
                          f"Успешно отправлено и записано: {counts['sent']}.\n"
                          f"Пропущено как дубли: {counts['duplicate']}.\n"
                          f"Ошибки до отправки: {counts['send_failed']}.\n"
                          f"Предыдущие неопределённые результаты: {counts['uncertain']}.\n"
                          f"Предыдущие результаты с ошибкой сохранения или требующие проверки: "
                          f"{sum(counts[k] for k in ('sent_db_save_failed', 'sent_db_update_failed', 'persistence_failed', 'manual_review'))}.\n"
                          f"Текущая остановленная запись: {index}/{len(items)}.\n"
                          f"Следующих получателей не обрабатывали: {len(items) - index}."),
                )
                return
            except TelegramSendPause as exc:
                queue['state'] = 'interrupted'
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(str(exc) + ' Очередь остановлена, следующие получатели не обрабатываются. '
                          'Результаты сохранены. Автоматического повтора не будет; '
                          'после паузы нужно проверить и восстановить только неотправленные сообщения.'),
                )
                return
            except AccessDenied as exc:
                queue["state"] = "interrupted"
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=("Отправка приостановлена: " + str(exc)
                          + " Очередь сохранена. После восстановления доступа используйте /queue."),
                )
                return
            except Exception as exc:
                status = "uncertain"
                details = (
                    f"Непредвиденная ошибка: "
                    f"{type(exc).__name__}: {exc}. "
                    "Автоматический повтор не выполняется."
                )

                try:
                    update_prepared_status(item, "uncertain", details)
                except Exception:
                    pass

            if status in counts:
                counts[status] += 1

            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Отправка {index}/{len(items)}\n"
                        f"ИНН: {item['inn']}\n"
                        f"Клиент: @{item['username']}\n"
                        f"Статус: {status}\n"
                        f"{details}"
                    ),
                )
            except Exception:
                pass

            if index < len(items):
                await asyncio.sleep(SEND_INTERVAL_SECONDS)

        summary = (
            "Отправка очереди завершена.\n\n"
            f"Всего в очереди: {len(items)}\n"
            f"Успешно отправлено и записано: {counts['sent']}\n"
            f"Пропущено как дубли: {counts['duplicate']}\n"
            f"Ошибки до отправки: {counts['send_failed']}\n"
            f"Неопределённый результат: {counts['uncertain']}\n"
            f"Отправлено, но обычная запись не создана: "
            f"{counts['sent_db_save_failed']}\n"
            f"Отправлено, но статус не обновлён: "
            f"{counts['sent_db_update_failed']}\n"
            f"Не удалось зафиксировать состояние: "
            f"{counts['persistence_failed']}\n"
            f"Ручная проверка: {counts['manual_review']}\n\n"
            "Перед любым повтором при неопределённом результате "
            "проверьте соответствующий чат вручную."
        )

        await application.bot.send_message(
            chat_id=chat_id,
            text=summary,
        )

        application.bot_data.pop(QUEUE_KEY, None)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        try:
            await application.bot.send_message(
                chat_id=queue["chat_id"],
                text=(
                    "Возникла ошибка при выполнении очереди. "
                    "Не запускайте повторную отправку, пока не проверите "
                    "статусы в SQLite и соответствующие Telegram-чаты.\n"
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        except Exception:
            pass

        queue["state"] = "interrupted"

    finally:
        from schedule_state import save, clear
        if application.bot_data.get(QUEUE_KEY) is queue:
            save(queue)
        else:
            clear()
        task = application.bot_data.get(SCHEDULE_TASK_KEY)

        if task is asyncio.current_task():
            application.bot_data.pop(SCHEDULE_TASK_KEY, None)


def create_bot() -> Application:
    init_database()
    load_allowed_users()

    if not config.BOT_TOKEN:
        raise RuntimeError("В .env не задан BOT_TOKEN.")

    application = Application.builder().token(
        config.BOT_TOKEN
    ).build()

    application.add_handler(CommandHandler("access", access_status_command))
    application.add_handler(CommandHandler('retry_research',retry_research_command))
    application.add_handler(CommandHandler('spam_reset', spam_reset_command))
    application.add_handler(
        CommandHandler("start", start_command)
    )
    application.add_handler(
        CommandHandler("request_access", request_access_command)
    )
    application.add_handler(
        CallbackQueryHandler(
            access_decision_callback,
            pattern=r"^access_(approve|reject)_[a-f0-9]{12}$",
        )
    )
    application.add_handler(
        CommandHandler("confirm", confirm_command)
    )
    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )
    application.add_handler(
        CommandHandler("queue", queue_command)
    )
    application.add_handler(
        CallbackQueryHandler(
            queue_callback,
            pattern=r"^queue_(confirm|cancel)$",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            schedule_time_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_excel,
        )
    )

    from service_reports_runtime import register as register_reports
    register_reports(application,root=Path.cwd(),allowed=user_is_allowed)
    from bot_menu import register as register_menu
    register_menu(application,role='sender',allowed=lambda u:u.effective_chat.type=='private' and user_is_allowed(u))
    return application
