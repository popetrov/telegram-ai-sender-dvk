"""One center process, separate administrative and machine-transport bots."""
import asyncio
from contextlib import AsyncExitStack
import logging
from telegram.error import NetworkError


async def run_center(admin, service, notices, admin_id, state, expected_username, *, directory=None, knowledge_clients=(),app_profiles=None):
    started, polling, tasks = [], [], []
    async with AsyncExitStack() as stack:
        try:
            for app in (admin, service):
                for attempt in range(3):
                    try:
                        await stack.enter_async_context(app)
                        break
                    except NetworkError:
                        if attempt == 2:
                            raise
                        logging.getLogger(__name__).warning('Telegram unavailable; retrying center connection')
                        await asyncio.sleep(3)
            if admin.bot.id == service.bot.id:
                raise RuntimeError('Административный и технический боты должны быть разными.')
            if service.bot.username.casefold() != expected_username.casefold():
                raise RuntimeError('Токен технического бота не соответствует настроенному имени.')
            for app in (admin, service):
                await app.start()
                started.append(app)
                def on_poll_error(exc, app=app):
                    app.create_task(app.process_error(update=None, error=exc))
                await app.updater.start_polling(bootstrap_retries=3, error_callback=on_poll_error)
                polling.append(app)
            tasks.append(asyncio.create_task(notices.run(admin.bot, admin_id)))
            if app_profiles is not None:
                tasks.append(asyncio.create_task(app_profiles.loop()))
            if directory is not None and (state / 'sheets.json').exists():
                from sheets_directory_sync import loop
                tasks.append(asyncio.create_task(loop(directory, state, notices)))
            if (state / 'backup.json').exists():
                from center_backup import loop
                tasks.append(asyncio.create_task(loop(state, state / 'backup.json')))
            print(f'Центр подключён. Управление: @{admin.bot.username}. '
                  f'Технический обмен: @{service.bot.username}.', flush=True)
            try:
                from bot_menu import publish
                await publish(admin,admin_id,'admin')
            except Exception as exc:
                logging.getLogger(__name__).warning('Owner menu available via /menu (%s)',type(exc).__name__)
            await asyncio.Event().wait()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Also stop an updater whose startup failed partway through.
            for app in reversed(started):
                if app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
            for resource in knowledge_clients:
                await resource.close()
