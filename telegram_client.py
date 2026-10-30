from safe_telegram import ProtectedTelegramClient as TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

import config


client = TelegramClient(
    config.SESSION_NAME,
    config.TELEGRAM_API_ID,
    config.TELEGRAM_API_HASH,
)


async def connect():
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram-сессия не авторизована.")

    me = await client.get_me()
    from app_profile_client import check_account
    from pathlib import Path
    check_account(__import__("installation_paths").state_root(),me.id)
    from account_guard import claim_account
    claim_account(me.id)

    print("=== TELEGRAM CONNECTION TEST ===")
    print(f"User ID: {me.id}")
    print(f"Phone: {me.phone}")


async def get_client_folders():
    result = await client(GetDialogFiltersRequest())

    client_folders = []

    for dialog_filter in result.filters:
        if not isinstance(dialog_filter, DialogFilter):
            continue

        title = dialog_filter.title.text

        if title == config.CLIENT_FOLDER_PREFIX:
            client_folders.append(dialog_filter)
            continue

        if title.startswith(config.CLIENT_FOLDER_PREFIX + " "):
            suffix = title[len(config.CLIENT_FOLDER_PREFIX) + 1:]

            if suffix.isdigit():
                client_folders.append(dialog_filter)

    client_folders.sort(
        key=lambda folder: (
            0
            if folder.title.text == config.CLIENT_FOLDER_PREFIX
            else int(folder.title.text.split(" ")[-1])
        )
    )

    return client_folders


async def get_folder_chats(dialog_filter):
    dialogs = []

    async for dialog in client.iter_dialogs():
        entity_id = dialog.entity.id

        include = False

        for included_id in dialog_filter.include_peers:
            if hasattr(included_id, "channel_id"):
                if entity_id == included_id.channel_id:
                    include = True
            elif hasattr(included_id, "user_id"):
                if entity_id == included_id.user_id:
                    include = True
            elif hasattr(included_id, "chat_id"):
                if entity_id == included_id.chat_id:
                    include = True

        if include:
            dialogs.append(dialog)

    return dialogs


async def test_client_folders():
    client_folders = await get_client_folders()

    print()
    print("=== CLIENT FOLDERS CONTENT ===")

    for folder in client_folders:
        chats = await get_folder_chats(folder)

        print()
        print(f"ПАПКА: {folder.title.text}")
        print(f"Количество чатов: {len(chats)}")

        for dialog in chats:
            print(
                f"  - {dialog.name} | "
                f"ID: {dialog.id}"
            )


async def disconnect():
    from account_guard import release_account
    try:
        if client.is_connected():
            await client.disconnect()
    finally:release_account()
