# Resolve relative database/session paths before importing clients.
import os
from pathlib import Path
os.chdir(Path(__file__).resolve().parent)
if __name__ == '__main__':
    if Path('data/security-install-pending.json').exists():
        raise SystemExit('Установка защиты не завершена. Повторите файл установки защиты; программа не запущена.')
    if Path('data/active-installation.json').exists():
        raise SystemExit('Эта папка уже обновлена. Запустите программу привычным start.bat (на Mac — start_mac.sh).')
    import atexit
    from file_lock import FileLock
    _process_lock = FileLock(Path('data/sender.lock'))
    _process_lock.__enter__()
    atexit.register(_process_lock.__exit__)
    import sys
    if '--self-check' in sys.argv:
        # A new installation legitimately has no API pair before center approval.
        # Check only local storage/catalog; do not construct Telegram clients.
        from database import init_database
        from ai import get_catalog
        init_database()
        get_catalog()
        print('Local database, imports and product catalog checked; no network calls.')
        raise SystemExit(0)
    if '--self-check' not in sys.argv:
        from app_profile_client import prepare
        prepare(Path.cwd(),sender_lock_held=True)
        from telegram_app_migrate import env_values
        _settings=env_values(Path('.env').read_text('utf-8-sig'))
        for _name in ('TELEGRAM_API_ID','TELEGRAM_API_HASH'):
            if _settings.get(_name):os.environ[_name]=_settings[_name]

import access_runtime
import sync_runtime
import asyncio

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
    from schedule_state import recover
    saved = recover()
    if saved:
        bot.bot_data[QUEUE_KEY] = saved
        print('Найдена прерванная очередь. Проверьте /queue; автоматическая отправка не возобновляется.')
    access_task = None
    sync_task = None
    spam_task = None
    dialogue_task = None
    safety_task = None

    try:
        await ensure_telegram_authorized()
        await connect()
        access_task = await access_runtime.start(client)
        sync_task = await sync_runtime.start(client)

        version = Path('version.txt').read_text('utf-8-sig').strip()
        print(f"=== TELEGRAM AI SENDER {version} ===")
        print("Основная Telegram-сессия подключена.")
        print("Excel-бот запущен.")
        print("Контроль безопасного обновления включён.")

        await bot.initialize()
        await bot.start()
        await bot.updater.start_polling()
        try:
            from bot_menu import publish as publish_menu
            await publish_menu(bot,access_runtime.account_id,'sender')
        except Exception as exc:
            print('Кнопки доступны через /menu ('+type(exc).__name__+').',flush=True)
        import spam_monitor
        async def notify_spam(text):
            await bot.bot.send_message(chat_id=access_runtime.account_id,text=text)
        from telegram_safety import SafetyNotifications
        safety = SafetyNotifications(client, notify_spam, access_runtime.account_id,
                                     config.MANAGER_ID, os.environ.get('ACCESS_BOT_USERNAME', ''),
                                     os.environ.get('ACCESS_PUBLIC_KEY', ''))
        safety_task = asyncio.create_task(safety.run(), name='telegram-safety-notifications')
        spam_task = await spam_monitor.start(client,notify_spam)
        try:
            import dialogue_runtime
            dialogue_task = await dialogue_runtime.start(client,bot)
        except Exception as exc:
            print('Автодиалоги не включены ('+type(exc).__name__+'). Первая часть продолжает работать.',flush=True)
            try:
                await notify_spam('Автодиалоги не включены. Проверьте настройку второй части; первая часть продолжает работать.')
            except Exception:
                pass

        try:
            await wait_for_safe_update(bot)
        finally:
            if bot.updater.running:
                await bot.updater.stop()

            if bot.running:
                await bot.stop()

            await bot.shutdown()

    finally:
        await access_runtime.stop(safety_task)
        await access_runtime.stop(dialogue_task)
        await access_runtime.stop(spam_task)
        await access_runtime.stop(access_task)
        await access_runtime.stop(sync_task)
        await disconnect()


if __name__ == "__main__":
    try:
        import sys
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановка по команде пользователя.")

