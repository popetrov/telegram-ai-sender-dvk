from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,Mock,patch

for name,value in dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='test',OPENAI_API_KEY='test',BOT_TOKEN='123:test',MANAGER_ID='test').items():
    os.environ.setdefault(name,value)
from dialogue_store import DialogueStore
from dialogue_runtime import read_settings,DialogueRuntime,original_text_matches
from dialogue_send_gate import DeliveryGate
import bot


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=DialogueStore(self.root/'test.sqlite',clock=lambda:1000);self.store.initialize()
        self.thread=self.store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='manager_001',account_id=42,peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
        self.store.record_message(self.thread,telegram_id=10,direction='outgoing',text='First',sent_at=100,source='history')
        self.store.mark_history_complete(self.thread)
        self.runtime=DialogueRuntime.__new__(DialogueRuntime)
        self.runtime.store=self.store;self.runtime.thread_id=self.thread;self.runtime.peer=77
        self.runtime.changed=__import__('asyncio').Event()
        self.runtime.gate=DeliveryGate(interval=0)
        self.runtime.refresh=AsyncMock(return_value=False)
        self.runtime.client=Object(send_message=AsyncMock(return_value=self.message(20,'Answer')))

    def message(self,id,text,out=True):
        return Object(id=id,raw_text=text,out=out,media=None,action=None,date=datetime.fromtimestamp(120,timezone.utc))

    def action(self):
        revision=self.store.snapshot(self.thread)[0]['revision']
        return self.store.queue_action(self.thread,action_key='reply:11',kind='reply',text='Answer',expected_revision=revision,due_at=999)

    def configured(self):
        path=self.root/'data/dialogue-runtime.json';path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(format=1,enabled=True,mode='pilot',business_id='dvk_finance',company_id='dvk',peer_id=77,product='БГ')),encoding='utf-8')
        return path

    def test_missing_settings_does_not_create_or_enable_anything(self):
        self.assertIsNone(read_settings(self.root))
        self.assertFalse((self.root/'data').exists())

    def test_old_markdown_rendering_is_accepted_but_changed_message_is_not(self):
        self.assertTrue(original_text_matches('Hello client','Hello **client**'))
        self.assertFalse(original_text_matches('Different message','Hello **client**'))

    def test_pilot_requires_explicit_one_peer_and_known_finance_product(self):
        path=self.configured();value=json.loads(path.read_text('utf-8'))
        self.assertEqual(read_settings(self.root)['peer_id'],77)
        for patch_value in ({'peer_id':None},{'mode':'all_chats'},{'product':'Николай'}):
            path.write_text(json.dumps({**value,**patch_value}),encoding='utf-8')
            with self.assertRaises(ValueError):read_settings(self.root)

    async def test_successful_send_is_recorded_and_never_replayed(self):
        action=self.action();row=self.store.action_for_key(self.thread,'reply:11')
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'):
            await self.runtime.send(row);await self.runtime.send(row)
        self.runtime.client.send_message.assert_awaited_once()
        result=self.store.action_for_key(self.thread,'reply:11')
        self.assertEqual(result['status'],'sent');self.assertEqual(result['telegram_id'],20)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'waiting')

    async def test_timeout_marks_uncertain_pauses_and_never_retries(self):
        self.action();row=self.store.action_for_key(self.thread,'reply:11')
        self.runtime.client.send_message.side_effect=TimeoutError()
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'):
            await self.runtime.send(row);await self.runtime.send(row)
        self.runtime.client.send_message.assert_awaited_once()
        self.assertEqual(self.store.action_for_key(self.thread,'reply:11')['status'],'uncertain')
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')
        self.assertEqual(len(self.store.pending_notices(42)),1)

    async def test_access_revocation_prevents_claim_and_network_send(self):
        self.action();row=self.store.action_for_key(self.thread,'reply:11')
        with patch('access_runtime.require_access',side_effect=PermissionError('revoked')):
            with self.assertRaises(PermissionError):await self.runtime.send(row)
        self.runtime.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.action_for_key(self.thread,'reply:11')['status'],'pending')

    async def test_message_arriving_during_send_delay_discards_stale_answer(self):
        self.action();row=self.store.action_for_key(self.thread,'reply:11')
        async def refresh():
            self.runtime.record(self.message(12,'Another question',out=False));return True
        self.runtime.refresh.side_effect=refresh
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'):
            await self.runtime.send(row)
        self.runtime.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.action_for_key(self.thread,'reply:11')['status'],'cancelled')

    def test_manual_outgoing_pauses_but_automation_does_not(self):
        self.runtime.record(self.message(12,'Manager answer'))
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')

    async def test_old_opt_out_is_adopted_without_sending_delayed_apology(self):
        self.runtime.record(self.message(11,'Do not write',out=False))
        self.runtime.engine=Object(analyst=Object(analyze=AsyncMock(return_value=dict(intent='opt_out'))))
        await self.runtime.adopt_history()
        self.assertEqual(self.store.thread(self.thread)['state'],'opt_out')
        self.assertEqual(self.store.pending_actions(42),[])
        self.runtime.client.send_message.assert_not_awaited()

    async def test_unread_attachment_stops_autoreply_and_alerts_manager(self):
        message=self.message(11,'Document',out=False);message.media=Object()
        self.runtime.record(message)
        self.assertEqual(self.store.thread(self.thread)['state'],'human')
        self.assertEqual(self.store.pending_notices(42)[0]['event_type'],'attachment')
