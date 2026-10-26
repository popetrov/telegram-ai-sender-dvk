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
from sync_data import next_transfer, acknowledge_transfer, TABLES

log = logging.getLogger(__name__)


def register_receiver(application, store, signing_key, encryption_key, notices=None):
    from telegram.ext import MessageHandler, filters
    upload_failed = False
    upload_seen = False

    async def receive(update, context):
        nonlocal upload_failed, upload_seen
        if update.effective_chat.type != 'private' or update.effective_message.caption != 'DVK_SYNC_V1':
            return
        document = update.effective_message.document
        if document.file_size is None or document.file_size > MAX_PACKET:
            return
        stage = 'get_file'
        try:
            async with asyncio.timeout(75):
                remote = await document.get_file(connect_timeout=15, read_timeout=30, pool_timeout=10)
                stage = 'download'
                content = await remote.download_as_bytearray(connect_timeout=15, read_timeout=45, pool_timeout=10)
            stage = 'decrypt_and_validate'
            payload = decrypt(encryption_key, content)
            subject, event_id, data = payload['subject'], payload['event_id'], payload['data']
            if (not isinstance(subject, str) or not isinstance(event_id, str) or len(event_id) != 32
                    or any(c not in '0123456789abcdef' for c in event_id)
                    or not isinstance(data, dict) or not isinstance(data.get('installation'), str)
                    or data.get('data', {}).get('table') not in (*TABLES, 'sync_fragment', 'sync_batch')):
                raise ValueError('Invalid sync data')
            digest = event_digest(data)
            stage = 'save'
            store.receive(update.effective_user.id, subject, event_id, digest, data)
            stage = 'receipt'
            await update.effective_message.reply_text('DVK_RECEIPT ' + json.dumps(
                receipt(signing_key, subject, event_id, digest), separators=(',', ':')), disable_notification=True)
            if upload_failed or not upload_seen:
                log.info('Encrypted data transfer working; records saved and acknowledged')
            upload_failed, upload_seen = False, True
        except Exception as exc:
            # Never put client records, tokens or decrypted data into logs.
            log.warning('Upload interrupted at %s (%s)', stage, type(exc).__name__)
            upload_failed = True
            if notices is not None:
                # Requests from unassigned accounts are ordinary onboarding, not alarms.
                managers = store.list_managers()
                account_id = update.effective_user.id
                if any(m['account_id'] == account_id and m['company_id'] for m in managers):
                    notices.error('приём данных', exc, account_id)

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
                    transfer = next_transfer(db)
                if transfer is None:
                    await asyncio.sleep(5)
                    continue
                event_ids, event_id, data = transfer
                for wire_id, wire_data in event_parts(event_id, data):
                    packet = encrypt(encryption, dict(subject=subject, event_id=wire_id, data=wire_data),
                                     compress=wire_data['data']['table'] == 'sync_batch')
                    stream = io.BytesIO(packet)
                    stream.name = 'encrypted.dvk'
                    async with transport_lock:
                        async with client.conversation(authority, timeout=180, exclusive=True) as conversation:
                            sent = await client.send_file(authority, stream, caption='DVK_SYNC_V1', force_document=True, silent=True)
                            async with asyncio.timeout(180):
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
                    await asyncio.sleep(2)
                with get_connection() as db:
                    acknowledge_transfer(db, event_ids)
                log.info('Sync confirmed: %d business records', len(event_ids))
                await asyncio.sleep(2)
            except Exception as exc:
                log.warning('Sync unavailable (%s); durable outbox retained', type(exc).__name__)
                await asyncio.sleep(60)
    return asyncio.create_task(poll(), name='owner-data-sync')
