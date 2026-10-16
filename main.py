import asyncio
from pathlib import Path

from getpass import getpass

from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions

import config
from telegram_client import client, connect, disconnect
from bot import (
    create_bot,
    QUEUE_KEY,
    PREPARING_KEY,
    SCHEDULE_TASK_KEY,
)


UPDATE_REQUEST = Path("data") / "update.request"
CHECK_INTERVAL = 2


def is_safe_to_update(application) -> tuple[bool, str]:
    """Разрешает обновление только при отсутствии активной работы."""

    bot_data = application.bot_data

    if bot_data.get(PREPARING_KEY):
        return False, "Выполняется подготовка сообщений."

    # Любая сохранённая в памяти очередь блокирует обновление,
    # в том числе прерванная: её нужно проверить вручную.
    if QUEUE_KEY in bot_data:
        queue = bot_data.get(QUEUE_KEY)
        state = queue.get("state", "неизвестно") if isinstance(queue, dict) else "неизвестно"
        return False, f"Очередь существует, состояние: {state}."

    scheduled_task = bot_data.get(SCHEDULE_TASK_KEY)

    if scheduled_task is not None and not scheduled_task.done():
        return False, "Существует активная задача отправки."

    return True, "Активных очередей и задач нет."


async def wait_for_safe_update(application):
    """Останавливает приложение только при наличии запроса обновления."""

    while True:
        if UPDATE_REQUEST.is_file():
            safe, reason = is_safe_to_update(application)

            if safe:
                print("Получен запрос на обновление.")
                print("Безопасное состояние подтверждено.")
                print("Завершаем приложение штатно.")
                return

            print(f"Обновление отложено: {reason}")

        await asyncio.sleep(CHECK_INTERVAL)


def _print_code_delivery(sent_code, stage):
    """Show Telegram's selected code-delivery route without exposing secrets."""
    current_type = getattr(sent_code, "type", None)
    print(
        f"{stage}: способ доставки кода — {type(current_type).__name__}.",
        flush=True,
    )

    next_type = getattr(sent_code, "next_type", None)
    if next_type is not None:
        print(
            "Следующий способ, который Telegram может разрешить: "
            f"{type(next_type).__name__}.",
            flush=True,
        )

    timeout = getattr(sent_code, "timeout", None)
    if timeout is not None:
        print(
            f"Интервал до следующего способа доставки: {timeout} сек.",
            flush=True,
        )


async def ensure_telegram_authorized():
    """Run first-time Telethon login in the console when no session exists."""
    await client.connect()

    if await client.is_user_authorized():
        return

    phone = (config.TELEGRAM_PHONE or "").strip()
    if not phone:
        phone = input(
            "Введите номер телефона Telegram в международном формате: "
        ).strip()

    if not phone:
        raise RuntimeError(
            "Номер телефона не указан. Заполните TELEGRAM_PHONE в .env."
        )

    print()
    print("Для первого запуска требуется авторизация Telegram.")
    try:
        sent_code = await client.send_code_request(phone)
    except Exception as exc:
        print(
            f"Telegram не принял запрос кода: {type(exc).__name__}: {exc}",
            flush=True,
        )
        raise

    _print_code_delivery(sent_code, "Первый запрос")

    code = input(
        "Введите код из Telegram (Enter — запросить повторную отправку): "
    ).strip()

    if not code:
        print("Запрашиваем повторную отправку через Telegram...")
        try:
            sent_code = await client(
                functions.auth.ResendCodeRequest(
                    phone,
                    sent_code.phone_code_hash,
                )
            )
        except Exception as exc:
            print(
                "Telegram отклонил повторную отправку: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            raise

        _print_code_delivery(sent_code, "Повторный запрос")
        code = input("Введите код из Telegram: ").strip()

    if not code:
        raise RuntimeError(
            "Код не введён. Авторизация остановлена; "
            "файл сессии не удалялся."
        )

    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=sent_code.phone_code_hash,
        )
    except SessionPasswordNeededError:
        password = getpass("Введите пароль двухэтапной проверки Telegram: ")
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Авторизация Telegram завершена. ID аккаунта: {me.id}")


async def main():
    bot = create_bot()

    try:
        await ensure_telegram_authorized()
        await connect()

        print("=== TELEGRAM AI SENDER 2.0.11 ===")
        print("Основная Telegram-сессия подключена.")
        print("Excel-бот запущен.")
        print("Контроль безопасного обновления включён.")

        await bot.initialize()
        await bot.start()
        await bot.updater.start_polling()

        try:
            await wait_for_safe_update(bot)
        finally:
            if bot.updater.running:
                await bot.updater.stop()

            if bot.running:
                await bot.stop()

            await bot.shutdown()

    finally:
        await disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановка по команде пользователя.")

