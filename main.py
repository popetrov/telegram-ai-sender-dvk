import asyncio

from pathlib import Path

from getpass import getpass

from telethon.errors import SessionPasswordNeededError

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


def _print_qr_code(url):
    """Render Telegram's temporary login QR directly in the Windows console."""
    try:
        import qrcode
    except ImportError:
        import subprocess
        import sys

        print("Устанавливаем компонент для отображения QR-кода...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "qrcode==8.2"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                "Не удалось установить компонент QR-кода. "
                "Проверьте доступ к PyPI и повторите запуск. "
                + (details[-1500:] or "")
            )
        import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def ensure_telegram_authorized():
    """Authorize an existing Telethon session or guide the manager through QR login."""
    await client.connect()

    if await client.is_user_authorized():
        return

    print()
    print("Для первого запуска требуется авторизация Telegram.")
    print("Откройте Telegram на телефоне: Настройки → Устройства → Подключить устройство.")
    print("Отсканируйте QR-код, который появится ниже.")
    print("Не отправляйте QR-код другим людям: он подтверждает вход в аккаунт.")
    print()

    while True:
        try:
            qr_login = await client.qr_login()
            _print_qr_code(qr_login.url)
            print("Ожидаем сканирования QR-кода (60 секунд)...", flush=True)
            await qr_login.wait(timeout=60)
            break
        except asyncio.TimeoutError:
            print("Время действия QR-кода истекло. Создаём новый...", flush=True)
        except SessionPasswordNeededError:
            password = getpass("Введите пароль двухэтапной проверки Telegram: ")
            await client.sign_in(password=password)
            break
        except Exception as exc:
            print(
                f"Не удалось выполнить QR-авторизацию: {type(exc).__name__}: {exc}",
                flush=True,
            )
            raise

    me = await client.get_me()
    print(f"Авторизация Telegram завершена. ID аккаунта: {me.id}", flush=True)


async def main():
    bot = create_bot()

    try:
        await ensure_telegram_authorized()
        await connect()

        print("=== TELEGRAM AI SENDER 2.0.16 ===")
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

