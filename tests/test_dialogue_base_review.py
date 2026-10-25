import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,Mock,patch
import test_dialogue_runtime
from dialogue_runtime import DialogueRuntime,DeletedHistory,read_settings,historical_contacts
from dialogue_store import DialogueStore,require_contact_allowed,ContactOptOutError
from dialogue_send_gate import DeliveryGate


class BaseReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=DialogueStore(self.root/'local.sqlite');self.store.initialize()
        with self.store.connection() as db:
            db.execute('CREATE TABLE local_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
        self.id=self.store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='me',account_id=42,
            peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
        self.record(10,'outgoing','First',100)
        self.store.mark_history_complete(self.id)
        self.runtime=DialogueRuntime.__new__(DialogueRuntime)
        self.runtime.store=self.store;self.runtime.thread_id=self.id;self.runtime.account_id=42;self.runtime.manager_id='me'
        self.runtime.peer=77;self.runtime.lock=asyncio.Lock();self.runtime.changed=asyncio.Event()
        self.runtime.settings=dict(mode='reviewed_base',review_id='a'*32,peer_id=77,product='БГ')
        self.runtime.engine=Object(analyst=Object(analyze=AsyncMock()),on_incoming=AsyncMock(),_check=Mock())
        self.runtime.client=Object(get_messages=AsyncMock(),get_input_entity=AsyncMock(return_value=77))

    def record(self,id,direction,text,date):
        self.store.record_message(self.id,telegram_id=id,direction=direction,text=text,sent_at=date,source='history' if direction=='outgoing' else 'client')

    async def test_silent_customer_uses_normal_cadence_and_review_does_not_send(self):
        await self.runtime.review_existing();await self.runtime.review_existing()
        self.runtime.engine.analyst.analyze.assert_not_awaited()
        self.runtime.engine.on_incoming.assert_not_awaited()
        self.assertEqual(self.store.pending_actions(42),[])
        self.assertEqual(self.store.thread(self.id)['state'],'waiting')

    async def test_unanswered_question_is_processed_once_with_normal_engine(self):
        self.record(11,'incoming','What documents are required?',110)
        self.runtime.engine.analyst.analyze.return_value=dict(intent='question',summary='Client question')
        await self.runtime.review_existing();await self.runtime.review_existing()
        self.runtime.engine.on_incoming.assert_awaited_once_with(self.id)

    async def test_progressed_dialogue_pauses_and_notifies_instead_of_sending(self):
        self.record(11,'incoming','Here are the details',110);self.record(12,'outgoing','Will check',120)
        await self.runtime.review_existing();await self.runtime.review_existing()
        self.assertEqual(self.store.thread(self.id)['state'],'human')
        self.assertEqual(len(self.store.pending_notices(42)),1)
        self.runtime.engine.on_incoming.assert_not_awaited()

    async def test_clear_need_is_sent_to_manager_not_replied_to_automatically(self):
        self.record(11,'incoming','Ready to apply',110)
        self.runtime.engine.analyst.analyze.return_value=dict(intent='ready',summary='Ready to apply')
        await self.runtime.review_existing()
        self.assertEqual(self.store.thread(self.id)['state'],'human')
        self.assertEqual(len(self.store.pending_notices(42)),1)
        self.runtime.engine.on_incoming.assert_not_awaited()

    async def test_old_opt_out_receives_no_delayed_apology(self):
        self.record(11,'incoming','Do not write again',110)
        self.runtime.engine.analyst.analyze.return_value=dict(intent='opt_out',summary='Stop')
        await self.runtime.review_existing()
        self.assertEqual(self.store.thread(self.id)['state'],'opt_out')
        self.assertEqual(self.store.pending_actions(42),[])

    async def test_deleted_known_message_cancels_pending_delivery_permanently(self):
        thread=self.store.thread(self.id)
        self.store.queue_action(self.id,action_key='reminder:1',kind='reminder',text='Follow-up',expected_revision=thread['revision'],due_at=1)
        self.runtime.client.get_messages.return_value=[None]
        self.assertFalse(await self.runtime.verify_visible_history())
        self.assertEqual(self.store.thread(self.id)['state'],'closed')
        self.assertEqual(self.store.pending_actions(42),[])
        with self.assertRaises(ContactOptOutError):require_contact_allowed(self.store.path,manager_id='me',peer_id=77)
        self.store.record_message(self.id,telegram_id=15,direction='outgoing',text='Manual message',sent_at=150,source='manager')
        self.assertEqual(self.store.thread(self.id)['state'],'closed')

    async def test_deleted_original_blocks_enrollment_and_closes_previous_thread(self):
        self.runtime.client.get_messages.return_value=None
        db=Object(execute=Mock(return_value=Object(fetchone=Mock(return_value={'telegram_message_id':10,'text':'First'}))))
        with patch('database.get_connection',return_value=nullcontext(db)),patch('access_runtime.require_access'):
            with self.assertRaises(DeletedHistory):await self.runtime.enroll()
        self.assertEqual(self.store.thread(self.id)['state'],'closed')

    async def test_message_deleted_during_preparation_is_never_sent(self):
        thread=self.store.thread(self.id)
        self.store.queue_action(self.id,action_key='reminder:1',kind='reminder',text='Follow-up',expected_revision=thread['revision'],due_at=1)
        row=self.store.action_for_key(self.id,'reminder:1')
        self.runtime.gate=DeliveryGate(interval=0);self.runtime.refresh=AsyncMock(return_value=False)
        self.runtime.client.get_messages.return_value=[None];self.runtime.client.send_message=AsyncMock()
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'):
            await self.runtime.send(row)
        self.runtime.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.thread(self.id)['state'],'closed')

    async def test_delete_event_is_scoped_to_actual_known_message_ids(self):
        await self.runtime.on_deleted(Object(chat_id=88,deleted_ids=[10]))
        await self.runtime.on_deleted(Object(chat_id=None,deleted_ids=[999]))
        self.assertEqual(self.store.thread(self.id)['state'],'waiting')
        await self.runtime.on_deleted(Object(chat_id=None,deleted_ids=[10]))
        self.assertEqual(self.store.thread(self.id)['state'],'closed')

    def test_base_scope_is_explicit_frozen_and_product_limited(self):
        (self.root/'data').mkdir();path=self.root/'data/dialogue-runtime.json'
        value=dict(format=1,enabled=True,mode='reviewed_base',contacts=[dict(peer_id=77,product='БГ')],
            review_id='a'*32,business_id='dvk_finance',company_id='dvk')
        path.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(read_settings(self.root)['mode'],'reviewed_base')
        value['contacts'].append(dict(peer_id=77,product='Кредит'));path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):read_settings(self.root)

    def test_historical_snapshot_excludes_other_managers_unsupported_and_unsent(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE clients(id,manager_id,telegram_id,product); CREATE TABLE messages(client_id,status,telegram_message_id);')
        db.executemany('INSERT INTO clients VALUES(?,?,?,?)',[(1,'me',11,'БГ'),(2,'other',22,'БГ'),(3,'me',33,'Law'),(4,'me',44,'Кредит')])
        db.executemany('INSERT INTO messages VALUES(?,?,?)',[(1,'sent',10),(2,'sent',20),(3,'sent',30),(4,'uncertain',40)])
        self.assertEqual(historical_contacts(db,'me'),[dict(peer_id=11,product='БГ')])


if __name__=='__main__':unittest.main()
