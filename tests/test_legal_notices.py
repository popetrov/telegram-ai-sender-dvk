import base64,json,os,tempfile
from pathlib import Path
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,patch
os.environ.setdefault('TELEGRAM_API_ID','12345')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from access_lease import canonical
from authority_store import AuthorityStore
from owner_notifications import OwnerNotifications
from access_server import register_research_notices
from legal_research import Journal,flush_notices
import config


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.key=Ed25519PrivateKey.generate()
        for field,value in [('DATABASE_PATH',str(self.root/'manager.sqlite')),('MANAGER_ID','manager_019')]:
            p=patch.object(config,field,value);p.start();self.addCleanup(p.stop)
        self.store=AuthorityStore(self.root/'center.sqlite');self.store.register(123,'manager_019:123')
        self.notices=OwnerNotifications(self.store);self.handlers=[]
        app=Object(add_handler=self.handlers.append)
        register_research_notices(app,self.store,self.key,self.notices)
        self.receive=self.handlers[0].callback
    def update(self,account=123,chat='private'):
        return Object(effective_user=Object(id=account),effective_chat=Object(type=chat),effective_message=Object(reply_text=AsyncMock()))
    def context(self,subject='manager_019:123',code='quota',notice='a'*64):
        payload=dict(subject=subject,code=code,notice_id=notice)
        return Object(args=[base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()])
    async def test_center_authorizes_identity_deduplicates_and_signs_ack(self):
        update=self.update()
        await self.receive(update,self.context())
        await self.receive(update,self.context(notice='b'*64))
        self.assertEqual(update.effective_message.reply_text.await_count,2)
        text=update.effective_message.reply_text.call_args.args[0]
        envelope=json.loads(text[len('DVK_RESEARCH_ACK '):])
        self.key.public_key().verify(base64.b64decode(envelope['signature']),canonical(envelope['payload']))
        with self.store.connection() as db:
            notices=db.execute('SELECT text FROM owner_notices').fetchall()
        self.assertEqual(len(notices),1);self.assertNotIn('API_KEY',notices[0][0])
    async def test_unknown_identity_group_and_unknown_error_are_ignored(self):
        for update,context in [(self.update(999),self.context()),(self.update(chat='group'),self.context()),
            (self.update(),self.context(subject='other:123')),(self.update(),self.context(code='arbitrary text'))]:
            await self.receive(update,context);update.effective_message.reply_text.assert_not_awaited()
    async def test_manager_marks_delivery_only_after_matching_signed_ack(self):
        journal=Journal(config.MANAGER_ID);journal.service_notice('quota')
        with journal.db() as db:notice=db.execute('SELECT notice_id FROM legal_service_notices').fetchone()[0]
        def ack(subject):
            payload=dict(protocol='DVK_RESEARCH_ACK_V1',subject=subject,notice_id=notice)
            envelope=dict(payload=payload,signature=base64.b64encode(self.key.sign(canonical(payload))).decode())
            return Object(raw_text='DVK_RESEARCH_ACK '+json.dumps(envelope))
        conversation=Object(send_message=AsyncMock(return_value=Object(id=1)),get_response=AsyncMock(side_effect=[ack('other:123'),ack('manager_019:123')]))
        class Context:
            async def __aenter__(self):return conversation
            async def __aexit__(self,*args):pass
        client=Object(conversation=lambda *args,**kwargs:Context())
        await flush_notices(client,'service','manager_019:123',self.key.public_key())
        with journal.db() as db:self.assertIsNotNone(db.execute('SELECT sent_at FROM legal_service_notices').fetchone()[0])
        wire=conversation.send_message.call_args.args[0]
        self.assertNotIn('ИНН',wire);self.assertNotIn('username',wire)
