"""Telegram document transport; data is encrypted before leaving the installation."""
import asyncio
import io
import json
import logging
import os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import base64
from access_runtime import transport_lock, cleanup_service_messages
from sync_protocol import (MAX_PACKET, decrypt, encrypt, receipt, event_digest, verify_receipt, event_parts)
from sync_data import next_event, TABLES

log = logging.getLogger(__name__)


def register_receiver(application, store, signing_key, encryption_key):
    from telegram.ext import MessageHandler, filters

    async def receive(update, context):
        if update.effective_chat.type != 'private' or update.effective_message.caption != 'DVK_SYNC_V1':
            return
        document = update.effective_message.document
        if document.file_size is None or document.file_size > MAX_PACKET:
            return
        try:
            remote = await document.get_file()
            content = await remote.download_as_bytearray()
            payload = decrypt(encryption_key, content)
            subject, event_id, data = payload['subject'], payload['event_id'], payload['data']
            if (not isinstance(subject, str) or not isinstance(event_id, str) or len(event_id) != 32
                    or any(c not in '0123456789abcdef' for c in event_id)
                    or not isinstance(data, dict) or not isinstance(data.get('installation'), str)
                    or data.get('data', {}).get('table') not in (*TABLES, 'sync_fragment')):
                raise ValueError('Invalid sync data')
            digest = event_digest(data)
            store.receive(update.effective_user.id, subject, event_id, digest, data)
            await update.effective_message.reply_text('DVK_RECEIPT ' + json.dumps(
                receipt(signing_key, subject, event_id, digest), separators=(',', ':')), disable_notification=True)
        except Exception as exc:
            # Never put client records, tokens or decrypted data into logs.
            log.warning('Upload rejected or interrupted (%s)', type(exc).__name__)

    application.add_handler(MessageHandler(filters.Document.ALL, receive))


async def start(client):
    authority = os.getenv('ACCESS_BOT_USERNAME', '').strip().lstrip('@')
    encryption = os.getenv('SYNC_PUBLIC_KEY', '').strip()
    public = os.getenv('ACCESS_PUBLIC_KEY', '').strip()
    if not authority or not encryption or not public:
        raise RuntimeError('Для передачи данных владельцу задайте SYNC_PUBLIC_KEY и параметры центра в .env')
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
    me = await client.get_me()
    subject = f"{os.environ['MANAGER_ID'].strip()}:{me.id}"

    async def poll():
        from database import get_connection
        while True:
            try:
                with get_connection() as db:
                    event = next_event(db)
                if event is None:
                    await asyncio.sleep(5)
                    continue
                event_id, data = event
                for wire_id, wire_data in event_parts(event_id, data):
                    packet = encrypt(encryption, dict(subject=subject, event_id=wire_id, data=wire_data))
                    stream = io.BytesIO(packet)
                    stream.name = 'encrypted.dvk'
                    async with transport_lock:
                        async with client.conversation(authority, timeout=60, exclusive=True) as conversation:
                            sent = await client.send_file(authority, stream, caption='DVK_SYNC_V1', force_document=True, silent=True)
                            async with asyncio.timeout(60):
                                while True:
                                    response = await conversation.get_response(sent)
                                    if not (response.raw_text or '').startswith('DVK_RECEIPT '):
                                        continue
                                    try:
                                        envelope = json.loads(response.raw_text[len('DVK_RECEIPT '):])
                                        verify_receipt(key, envelope, subject, wire_id, event_digest(wire_data))
                                        break
                                    except Exception:
                                        continue
                            await cleanup_service_messages(client, authority, sent, response)
                    await asyncio.sleep(2)
                with get_connection() as db:
                    db.execute('DELETE FROM sync_outbox WHERE event_id=?', (event_id,))
                await asyncio.sleep(2)
            except Exception as exc:
                log.warning('Sync unavailable (%s); durable outbox retained', type(exc).__name__)
                await asyncio.sleep(60)
    return asyncio.create_task(poll(), name='owner-data-sync')
