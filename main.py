import asyncio

from telegram_client import connect, disconnect
from bot import create_bot


async def main():
    bot = create_bot()

    try:
        await connect()

        print("=== TELEGRAM SENDER ===")
        print("Основная Telegram-сессия подключена.")
        print("Excel-бот запущен.")

        await bot.initialize()
        await bot.start()
        await bot.updater.start_polling()

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await bot.updater.stop()
            await bot.stop()
            await bot.shutdown()

    finally:
        await disconnect()


if __name__ == "__main__":
    asyncio.run(main())