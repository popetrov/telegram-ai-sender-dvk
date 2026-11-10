"""Addressed, encrypted pull and single-use CRM dispatch permits for the pilot."""

import asyncio
import base64
import io
import json
import logging
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from access_lease import canonical
from sync_protocol import MAX_PACKET, b64, decrypt, encrypt, event_digest, public_text


REQUEST = 'DVK_BITRIX_FOLLOWUP_V1'
RESPONSE = 'DVK_BITRIX_FOLLOWUP_RESULT_V1'
log = logging.getLogger(__name__)


def make_request(public, subject, action, job=None):
    private = X25519PrivateKey.generate()
    data = dict(protocol=REQUEST, subject=subject, action=action, job=job,
                request_id=uuid.uuid4().hex, created=int(time.time()),
                reply_key=public_text(private))
    return data, private, encrypt(public, data)


def read_result(packet, request, private, signing):
    signed = decrypt(private, packet)
    data = signed['payload']
    signing.verify(base64.b64decode(signed['signature'], validate=True), canonical(data))
    if (data.get('protocol') != RESPONSE or data.get('request_digest') != event_digest(request)
            or type(data.get('created')) is not int or abs(time.time() - data['created']) > 60):
        raise ValueError('Ответ центра устарел или относится к другому запросу.')
    return data['result']


class Broker:
    def __init__(self, store, signing, encryption, jobs, claims, check_crm, *, account, manager, company):
        self.account, self.subject, self.company = account, f"{manager}:{account}", company
        self.store, self.signing, self.encryption = store, signing, encryption
        self.jobs, self.claims, self.check_crm = jobs, claims, check_crm

    def allowed(self, account):
        if account != self.account or not self.store.permission(account, self.subject)[0]:
            return False
        return any(r['account_id'] == self.account and r['subject'] == self.subject
                   and r['company_id'] == self.company for r in self.store.list_managers())

    async def handle(self, packet, account):
        if not self.allowed(account):
            raise PermissionError('Account is outside the Bitrix pilot')
        request = decrypt(self.encryption, packet)
        if (not isinstance(request, dict)
                or set(request) != {'protocol','subject','action','job','request_id','created','reply_key'}
                or request['protocol'] != REQUEST or request['subject'] != self.subject
                or type(request['created']) is not int or abs(time.time() - request['created']) > 90
                or not isinstance(request['request_id'], str) or len(request['request_id']) != 32
                or len(base64.b64decode(request['reply_key'], validate=True)) != 32):
            raise ValueError('Invalid follow-up request')
        result = {'ok': False}
        if request['action'] == 'list' and request['job'] is None:
            result = {'ok': True, 'jobs': [job for key, job in self.jobs.items()
                                          if not (self.claims / key).exists()]}
        elif request['action'] == 'claim' and isinstance(request['job'], dict):
            ref = request['job']
            key = ref.get('id')
            job = self.jobs.get(key) if isinstance(key, str) else None
            if job and ref == {'id': key, 'digest': event_digest(job)}:
                path = self.claims / key
                if not path.exists() and await asyncio.to_thread(self.check_crm, job) and self.allowed(account):
                    try:
                        with path.open('x', encoding='utf-8') as file:
                            json.dump({'created': int(time.time()), 'request': request['request_id']}, file)
                    except FileExistsError:
                        pass
                    else:
                        result = {'ok': True, 'job': key}
        response = dict(protocol=RESPONSE, request_digest=event_digest(request),
                        created=int(time.time()), result=result)
        signed = dict(payload=response, signature=b64(self.signing.sign(canonical(response))))
        return encrypt(request['reply_key'], signed)


def register_receiver(application, broker):
    from telegram.ext import MessageHandler, filters

    gate = asyncio.Semaphore(2)

    async def process(update, context):
        try:
            msg = update.effective_message
            if (update.effective_chat.type != 'private' or update.effective_user.id != broker.account
                    or not msg.document.file_size or msg.document.file_size > MAX_PACKET):
                return
            file = await msg.document.get_file()
            async with gate:
                raw = await file.download_as_bytearray()
                answer = await broker.handle(raw, update.effective_user.id)
            stream = io.BytesIO(answer)
            stream.name = 'bitrix-followup-result.dvk'
            await msg.reply_document(stream, caption=RESPONSE, disable_notification=True, do_quote=True)
        except Exception as exc:
            log.warning('Bitrix follow-up request stopped (%s)', type(exc).__name__)

    async def receive(update, context):
        application.create_task(process(update, context), update=update)

    application.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex('^' + REQUEST + '$'), receive),
        group=-1,
    )


async def call_center(client, authority, encryption, signing, lock, access, subject, action, job=None):
    access()
    request, private, packet = make_request(encryption, subject, action, job)
    async with lock:
        access()
        stream = io.BytesIO(packet)
        stream.name = 'bitrix-followup.dvk'
        async with client.conversation(authority, timeout=90, exclusive=True) as conversation:
            sent = await client.send_file(authority, stream, caption=REQUEST, force_document=True, silent=True)
            async with asyncio.timeout(90):
                while True:
                    reply = await conversation.get_response(sent)
                    if reply.raw_text != RESPONSE or not reply.document or reply.reply_to_msg_id != sent.id:
                        continue
                    if reply.document.size > MAX_PACKET:
                        raise ValueError('Ответ центра слишком большой.')
                    packet = await client.download_media(reply, file=bytes)
                    result = read_result(packet, request, private, signing)
                    access()
                    return result
