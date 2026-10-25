import asyncio
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,Mock,patch
import test_dialogue_runtime  # sets required test-only environment
from dialogue_runtime import eligible_contacts,read_settings,SelectedQueueRuntime,should_reply_at_activation
from dialogue_send_gate import DeliveryGate
from dialogue_manager_ui import register
from dialogue_store import DialogueStore


class CohortTests(unittest.IsolatedAsyncioTestCase):
    def test_only_own_sent_finance_contacts_with_one_product_qualify(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE clients(id,manager_id,telegram_id,product,inn,username); CREATE TABLE messages(client_id,status,telegram_message_id,text,sent_at); CREATE TABLE prepared_messages(id,manager_id,inn,username,product,final_message,status,created_at);')
        for i,(manager,peer,product,status,message) in enumerate([
                ('me',1,'БГ','sent',10),('me',2,'Кредит','sent',20),
                ('other',3,'БГ','sent',30),('me',4,'БГ','uncertain',40),
                ('me',5,'Право','sent',50),('me',6,'БГ','sent',None),
                ('me',7,'БГ','sent',70),('me',7,'Кредит','sent',71),
                ('me',None,'БГ','sent',80)]):
            db.execute('INSERT INTO clients VALUES(?,?,?,?,?,?)',(i,manager,peer,product,str(i),str(i)))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(i,status,message,'first',200))
            db.execute('INSERT INTO prepared_messages VALUES(?,?,?,?,?,?,?,?)',(i+1,manager,str(i),str(i),product,'first','sent',100))
        self.assertEqual(eligible_contacts(db,'me',list(range(1,10))),[(1,'БГ'),(2,'Кредит')])
        # A sent historical contact outside the exact chosen queue never joins.
        self.assertEqual(eligible_contacts(db,'me',[2]),[(2,'Кредит')])
        self.assertEqual(eligible_contacts(db,'me',[]),[])
        db.execute("UPDATE prepared_messages SET status='uncertain' WHERE id=2")
        self.assertEqual(eligible_contacts(db,'me',[2]),[])
        db.execute("UPDATE prepared_messages SET status='sent',final_message='different' WHERE id=2")
        self.assertEqual(eligible_contacts(db,'me',[2]),[])

    def test_cohort_is_explicit_and_does_not_enable_other_products(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            path=root/'data/dialogue-runtime.json'
            value=dict(format=1,enabled=True,mode='selected_queue',prepared_ids=[1,2],business_id='dvk_finance',company_id='dvk',activated_at=1000)
            path.write_text(json.dumps(value),encoding='utf-8')
            self.assertEqual(read_settings(root)['mode'],'selected_queue')
            value['prepared_ids'].append(-1);path.write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaises(ValueError):read_settings(root)

    def test_quick_queue_reply_is_processed_but_old_history_is_not_replied_to(self):
        settings={'mode':'selected_queue','activated_at':1000}
        self.assertFalse(should_reply_at_activation(settings,[{'direction':'incoming','sent_at':999}]))
        self.assertTrue(should_reply_at_activation(settings,[{'direction':'incoming','sent_at':1001}]))
        self.assertFalse(should_reply_at_activation({'mode':'pilot'},[{'direction':'incoming','sent_at':1001}]))

    async def test_paged_controls_do_not_dump_hundreds_of_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DialogueStore(Path(folder)/'local.sqlite');store.initialize()
            for peer in range(25):
                store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='me',account_id=42,
                    peer_id=peer+1,product='БГ',first_message_id=peer+1,first_sent_at=100)
            app=Object(add_handler=Mock());register(app,store,account_id=42,manager_id='me')
            callback=app.add_handler.call_args_list[1].args[0].callback
            reply=AsyncMock()
            await callback(Object(effective_user=Object(id=42),effective_chat=Object(type='private'),
                                  effective_message=Object(reply_text=reply)),Object(args=['3']))
            self.assertEqual(reply.await_count,6)  # summary and last five threads
            self.assertIn('3/3',reply.call_args_list[0].args[0])

    async def test_scheduler_prioritizes_incoming_and_shares_delivery_lane(self):
        runtime=SelectedQueueRuntime.__new__(SelectedQueueRuntime)
        runtime.account_id=42;runtime.scanned_at={77:__import__('time').monotonic()}
        action={'thread_id':'a'}
        worker=Object(lock=asyncio.Lock(),settings={'peer_id':77},changed=asyncio.Event(),
            refresh=AsyncMock(return_value=False),thread_id='a',engine=Object(on_incoming=AsyncMock(),
            on_silence=AsyncMock(),on_return=AsyncMock()),store=Object(pending_actions=Mock(return_value=[action])),send=AsyncMock())
        worker.changed.set()
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'),patch('dialogue_runtime.asyncio.sleep',new=AsyncMock()):
            await runtime.service(worker)
        worker.engine.on_incoming.assert_awaited_once_with('a')
        worker.send.assert_awaited_once_with(action)
        self.assertFalse(worker.changed.is_set())

    async def test_actions_for_another_peer_are_never_sent_by_this_worker(self):
        runtime=SelectedQueueRuntime.__new__(SelectedQueueRuntime)
        runtime.account_id=42;runtime.scanned_at={77:__import__('time').monotonic()}
        worker=Object(lock=asyncio.Lock(),settings={'peer_id':77},changed=asyncio.Event(),thread_id='a',
            engine=Object(on_incoming=AsyncMock(),on_silence=AsyncMock(),on_return=AsyncMock()),
            store=Object(pending_actions=Mock(return_value=[{'thread_id':'another'}])),send=AsyncMock())
        with patch('access_runtime.require_access'),patch('bot.require_send_cooldown_finished'):
            await runtime.service(worker)
        worker.send.assert_not_awaited()

    async def test_stop_restores_first_contact_gate_and_closes_ai(self):
        import bot
        runtime=SelectedQueueRuntime.__new__(SelectedQueueRuntime)
        runtime.store=Mock();runtime.account_id=42;runtime.manager_id='me';runtime.handler=None
        runtime.settings={'mode':'selected_queue','prepared_ids':[1,2]}
        runtime.delete_handler=None
        runtime.client=Object(add_event_handler=Mock(),remove_event_handler=Mock())
        runtime.application=Object(add_handler=Mock(),bot=Object(send_message=AsyncMock(side_effect=asyncio.CancelledError())))
        runtime.ai_client=Object(close=AsyncMock());runtime.previous_gate=DeliveryGate()
        with patch.object(bot,'delivery_gate',DeliveryGate()):
            with self.assertRaises(asyncio.CancelledError):await runtime.run()
            self.assertIs(bot.delivery_gate,runtime.previous_gate)
        runtime.ai_client.close.assert_awaited_once()
        self.assertEqual(runtime.client.remove_event_handler.call_count,2)


if __name__=='__main__':unittest.main()
