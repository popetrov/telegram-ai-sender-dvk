import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,Mock,patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from authority_store import AuthorityStore
from dialogue_store import DialogueStore
from dialogue_manager_ui import register,deliver_notices
from dialogue_knowledge import Evidence,KnowledgeAnswer
from dialogue_knowledge_protocol import KnowledgeBroker,RESPONSE_PROTOCOL
from dialogue_knowledge_transport import RemoteKnowledge,register_receiver
from sync_protocol import public_text


class UITransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_private_chat_reply_explicitly_binds_knowledge_result_to_request(self):
        from datetime import datetime,timezone
        from telegram import Message,Chat,Document,User
        document=Document('file','unique',file_size=100)
        network=Object(defaults=None,get_file=AsyncMock(return_value=Object(download_as_bytearray=AsyncMock(return_value=b'packet'))),
                       send_document=AsyncMock())
        document.set_bot(network)
        message=Message(100,datetime.now(timezone.utc),Chat(42,'private'),document=document)
        message.set_bot(network)
        tasks=[]
        def create(coro,**kwargs):tasks.append(asyncio.create_task(coro))
        app=Object(add_handler=Mock(),create_task=create)
        broker=Object(handle=AsyncMock(return_value=b'signed-encrypted-result'))
        register_receiver(app,broker)
        receive=app.add_handler.call_args.args[0].callback
        await receive(Object(effective_message=message,effective_chat=message.chat,effective_user=User(42,'Owner',False)),None)
        await asyncio.gather(*tasks)
        parameters=network.send_document.call_args.kwargs['reply_parameters']
        self.assertIsNotNone(parameters)
        self.assertEqual(parameters.message_id,100)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.store=DialogueStore(root/'local.sqlite',clock=lambda:1000);self.store.initialize()
        self.thread=self.store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='manager_001',account_id=42,peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
        self.application=Object(add_handler=Mock())
        register(self.application,self.store,account_id=42,manager_id='manager_001')
        self.callback=self.application.add_handler.call_args_list[0].args[0].callback
        self.query=Object(data=f'dlg:p:{self.thread}:0',answer=AsyncMock(),edit_message_reply_markup=AsyncMock())

    async def test_other_user_cannot_take_control(self):
        update=Object(callback_query=self.query,effective_user=Object(id=43),effective_chat=Object(type='private'))
        await self.callback(update,None)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'waiting')
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_buttons_pause_then_resume_without_sending_to_client(self):
        update=Object(callback_query=self.query,effective_user=Object(id=42),effective_chat=Object(type='private'))
        await self.callback(update,None)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')
        self.query.data=f'dlg:r:{self.thread}:1'
        await self.callback(update,None)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'engaged')

    async def test_failed_notice_delivery_stays_pending_for_same_manager(self):
        self.store.handoff(self.thread,event_key='need:1',event_type='need',summary='Needs guarantee',pause=False,expected_revision=0)
        bot=Object(send_message=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaises(TimeoutError):await deliver_notices(bot,self.store,account_id=42)
        self.assertEqual(len(self.store.pending_notices(42)),1)
        bot.send_message.side_effect=None
        await deliver_notices(bot,self.store,account_id=42)
        self.assertEqual(bot.send_message.call_args.kwargs['chat_id'],42)
        self.assertEqual(self.store.pending_notices(42),[])

    async def test_encrypted_technical_transport_round_trip_never_sends_customer_message(self):
        authority=AuthorityStore(Path(self.temp.name)/'center.sqlite')
        authority.register(42,'manager_001:42');authority.assign(42,'dvk')
        signing=Ed25519PrivateKey.generate();encryption=X25519PrivateKey.generate()
        provider=Object(products={'БГ'},lookup=AsyncMock(return_value=KnowledgeAnswer('Fact',(Evidence('1','file','source','Long confirmed fact'),),'req')))
        broker=KnowledgeBroker(authority,{('dvk','dvk_finance'):provider},signing,encryption)
        state={}
        async def upload(peer,stream,**kwargs):
            self.assertEqual(peer,'service_bot')
            state['response']=await broker.handle(stream.getvalue(),account_id=42)
            return Object(id=100)
        conversation=Object(get_response=AsyncMock(return_value=Object(raw_text=RESPONSE_PROTOCOL,document=Object(size=1000),reply_to_msg_id=100)))
        class Context:
            async def __aenter__(self):return conversation
            async def __aexit__(self,*args):return False
        client=Object(send_file=AsyncMock(side_effect=upload),conversation=Mock(return_value=Context()),
                      download_media=AsyncMock(side_effect=lambda *a,**kw:state['response']))
        remote=RemoteKnowledge(client,authority='service_bot',subject='manager_001:42',encryption_public_key=public_text(encryption),
                               signing_public_key=signing.public_key(),transport_lock=asyncio.Lock(),require_access=Mock())
        request_id='a'*32
        with patch('access_runtime.cleanup_service_messages',new_callable=AsyncMock):
            answer=await remote.lookup(business_id='dvk_finance',product='БГ',question='Question',request_id=request_id)
        self.assertEqual(answer.request_id,request_id)
        self.assertEqual(answer.answer,'Fact')
        self.assertEqual(client.send_file.await_count,1)
        self.assertFalse(hasattr(client,'send_message'))
