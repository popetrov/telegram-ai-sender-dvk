import json
from pathlib import Path
import tempfile
import hashlib
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock,Mock
from dialogue_policy import DialoguePolicy
from dialogue_store import DialogueStore,StaleDialogue
from dialogue_engine import DialogueEngine
from dialogue_knowledge import KnowledgeUnavailable


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=2000000
        self.store=DialogueStore(Path(self.temp.name)/'test.sqlite',clock=lambda:self.now);self.store.initialize()
        value=json.loads((Path(__file__).resolve().parents[1]/'dialogue_profiles/dvk_finance.json').read_text('utf-8'))
        value['enabled']=True;self.policy=DialoguePolicy(value)
        self.thread=self.store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='manager_001',account_id=42,peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
        self.store.record_message(self.thread,telegram_id=10,direction='outgoing',text='First contact',sent_at=100,source='history')
        self.store.mark_history_complete(self.thread)
        self.analyst=Object(analyze=AsyncMock(return_value=dict(intent='question',summary='Question',knowledge_question='How to apply?')))
        self.knowledge=Object(lookup=AsyncMock(return_value=Object(answer='Confirmed answer')))
        self.writer=Object(compose=AsyncMock(return_value='Customer response'))
        self.guard=Mock()
        self.reply_key='reply:11:'+hashlib.sha256(b'Customer reply').hexdigest()[:12]
        self.engine=DialogueEngine(self.store,self.policy,analyst=self.analyst,knowledge=self.knowledge,writer=self.writer,require_access=self.guard,clock=lambda:self.now)

    def incoming(self):
        self.store.record_message(self.thread,telegram_id=11,direction='incoming',text='Customer reply',sent_at=110,source='client')

    async def test_question_uses_complete_history_and_queues_only_one_answer(self):
        self.incoming();action=await self.engine.on_incoming(self.thread)
        self.assertIsNotNone(action)
        self.assertEqual(len(self.writer.compose.call_args.kwargs['history']),2)
        self.assertIsNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.analyst.analyze.await_count,1)

    async def test_opt_out_never_calls_knowledge_and_cancels_all_followups(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='opt_out',summary='Asked not to write')
        action=await self.engine.on_incoming(self.thread)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'opt_out')
        self.assertEqual(self.store.claim_action(action)['kind'],'opt_out_ack')
        self.assertIsNone(await self.engine.on_silence(self.thread))
        self.knowledge.lookup.assert_not_awaited()

    async def test_ready_pauses_while_call_only_notifies_and_continues_answering(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='call',summary='Requested call',knowledge_question='How to request a call?')
        self.assertIsNotNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'engaged')
        self.assertEqual(self.store.pending_notices(42)[0]['event_type'],'call')

    async def test_missing_knowledge_pauses_and_notifies_instead_of_inventing_answer(self):
        self.incoming();self.knowledge.lookup.side_effect=KnowledgeUnavailable('No source')
        self.assertIsNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')
        self.assertEqual(self.store.pending_notices(42)[0]['event_type'],'knowledge_missing')
        self.writer.compose.assert_not_awaited()

    async def test_simple_callback_request_notifies_without_unnecessary_knowledge_lookup(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='call',summary='Requested call',knowledge_question='')
        self.assertIsNotNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.store.thread(self.thread)['state'],'engaged')
        self.knowledge.lookup.assert_not_awaited()
        self.assertEqual(self.store.pending_notices(42)[0]['event_type'],'call')

    async def test_repeated_callback_request_does_not_spam_manager(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='call',summary='Requested call',knowledge_question='')
        await self.engine.on_incoming(self.thread)
        self.store.record_message(self.thread,telegram_id=12,direction='incoming',text='Please call again',sent_at=111,source='client')
        self.analyst.analyze.return_value=dict(intent='call',summary='Repeated call request',knowledge_question='')
        await self.engine.on_incoming(self.thread)
        self.assertEqual(len(self.store.pending_notices(42)),1)

    async def test_ready_to_work_stops_without_trying_to_continue_selling(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='ready',summary='Ready to apply')
        self.assertIsNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')
        self.assertEqual(self.store.pending_notices(42)[0]['event_type'],'ready')
        self.knowledge.lookup.assert_not_awaited();self.writer.compose.assert_not_awaited()

    async def test_manager_takeover_during_generation_discards_answer(self):
        self.incoming()
        async def compose(**kwargs):
            revision=self.store.snapshot(self.thread)[0]['revision']
            self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='pause',expected_revision=revision)
            return 'Stale answer'
        self.writer.compose.side_effect=compose
        with self.assertRaises(StaleDialogue):await self.engine.on_incoming(self.thread)
        self.assertIsNone(self.store.action_for_key(self.thread,self.reply_key))

    async def test_silence_schedules_single_step_and_stops_when_customer_answers(self):
        action=await self.engine.on_silence(self.thread)
        self.assertEqual(self.store.claim_action(action)['kind'],'reminder')
        self.store.finish_action(action,status='sent',telegram_id=20)
        self.assertIsNone(await self.engine.on_silence(self.thread))
        self.incoming()
        self.now+=40000000
        self.assertIsNone(await self.engine.on_silence(self.thread))

    async def test_disabled_profile_or_lost_access_never_calls_ai(self):
        self.policy.enabled=False
        with self.assertRaises(PermissionError):await self.engine.on_incoming(self.thread)
        self.policy.enabled=True;self.guard.side_effect=PermissionError('Access revoked')
        with self.assertRaises(PermissionError):await self.engine.on_silence(self.thread)
        self.analyst.analyze.assert_not_awaited();self.knowledge.lookup.assert_not_awaited()

    async def test_return_later_uses_agreed_date_then_sends_only_one_reconnect(self):
        self.incoming();self.analyst.analyze.return_value=dict(intent='later',summary='Asked to return',return_at=self.now+100)
        self.assertIsNotNone(await self.engine.on_incoming(self.thread))
        self.assertEqual(self.store.thread(self.thread)['state'],'later')
        self.assertIsNone(await self.engine.on_return(self.thread))
        self.now+=101
        self.assertIsNotNone(await self.engine.on_return(self.thread))
        self.assertEqual(self.store.thread(self.thread)['state'],'engaged')
        self.assertIsNone(await self.engine.on_return(self.thread))
