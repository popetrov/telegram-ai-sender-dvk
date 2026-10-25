"""Encrypted knowledge requests through the technical bot, separate from sync."""
import asyncio
import io
import logging
from dialogue_knowledge import KnowledgeUnavailable,KnowledgeTransportUnavailable
from dialogue_knowledge_protocol import REQUEST_PROTOCOL,RESPONSE_PROTOCOL,make_request,read_response
from sync_protocol import MAX_PACKET

log=logging.getLogger(__name__)


class RemoteKnowledge:
    def __init__(self,client,*,authority,subject,encryption_public_key,signing_public_key,transport_lock,require_access):
        self.client,self.authority,self.subject=client,authority,subject
        self.encryption_public_key,self.signing_public_key=encryption_public_key,signing_public_key
        self.lock,self.require_access=transport_lock,require_access

    async def lookup(self,*,business_id,product,question,request_id):
        self.require_access()
        request,private,packet=make_request(self.encryption_public_key,subject=self.subject,
                                           business_id=business_id,product=product,question=question,request_id=request_id)
        try:
            async with self.lock:
                self.require_access()
                stream=io.BytesIO(packet);stream.name='knowledge.dvk'
                async with self.client.conversation(self.authority,timeout=180,exclusive=True) as conversation:
                    sent=await self.client.send_file(self.authority,stream,caption=REQUEST_PROTOCOL,force_document=True,silent=True)
                    async with asyncio.timeout(180):
                        while True:
                            response=await conversation.get_response(sent)
                            if (response.raw_text!=RESPONSE_PROTOCOL or not response.document
                                    or response.reply_to_msg_id!=sent.id):
                                continue
                            if response.document.size>MAX_PACKET:
                                raise KnowledgeUnavailable('Knowledge document exceeds limit')
                            raw=await self.client.download_media(response,file=bytes)
                            result=read_response(raw,request=request,private_key=private,signing_public_key=self.signing_public_key)
                            self.require_access()
                            from access_runtime import cleanup_service_messages
                            await cleanup_service_messages(self.client,self.authority,sent,response)
                            return result
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            from telethon.errors import FloodWaitError,PeerFloodError
            if isinstance(exc,(FloodWaitError,PeerFloodError)):
                import bot
                if isinstance(exc,PeerFloodError):bot.pause_for_spam()
                else:bot.pause_for_flood(exc)
            raise KnowledgeTransportUnavailable('Center knowledge request failed: '+type(exc).__name__) from None


def register_receiver(application,broker):
    from telegram.ext import MessageHandler,filters
    semaphore=asyncio.Semaphore(3)

    async def process(update,context):
        try:
            if update.effective_chat.type!='private':return
            document=update.effective_message.document
            if document.file_size is None or document.file_size>MAX_PACKET:return
            async with asyncio.timeout(90):
                remote=await document.get_file(connect_timeout=15,read_timeout=30,pool_timeout=10)
                raw=await remote.download_as_bytearray(connect_timeout=15,read_timeout=45,pool_timeout=10)
            async with semaphore:
                result=await broker.handle(raw,account_id=update.effective_user.id)
            stream=io.BytesIO(result);stream.name='knowledge-result.dvk'
            await update.effective_message.reply_document(stream,caption=RESPONSE_PROTOCOL,disable_notification=True,do_quote=True,
                                                           connect_timeout=15,read_timeout=45,write_timeout=45)
        except Exception as exc:
            log.warning('Knowledge exchange interrupted (%s)',type(exc).__name__)

    async def receive(update,context):
        application.create_task(process(update,context),update=update)

    application.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex('^'+REQUEST_PROTOCOL+'$'),receive),group=-1)
