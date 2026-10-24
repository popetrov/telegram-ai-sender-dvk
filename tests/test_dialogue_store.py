from pathlib import Path
import tempfile
import unittest
from dialogue_store import DialogueStore,StaleDialogue


class DialogueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'dialogue.sqlite'
        self.store=DialogueStore(self.path,clock=lambda:1000);self.store.initialize()
        self.binding=dict(company_id='dvk',business_id='dvk_finance',manager_id='manager_001',
                          account_id=42,peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
        self.thread=self.store.enroll(**self.binding)

    def history(self):
        self.store.record_message(self.thread,telegram_id=10,direction='outgoing',text='First contact',sent_at=100,source='history')
        self.store.mark_history_complete(self.thread)
        return self.store.snapshot(self.thread)[0]['revision']

    def action(self,revision,**kwargs):
        return self.store.queue_action(self.thread,action_key='reply:11',kind='reply',text='Answer',
                                       expected_revision=revision,due_at=999,**kwargs)

    def test_binding_is_idempotent_and_cannot_mix_business_or_manager(self):
        self.assertEqual(self.thread,self.store.enroll(**self.binding))
        for field,value in [('company_id','other'),('manager_id','manager_002'),('product','Кредит')]:
            with self.assertRaises(ValueError):self.store.enroll(**{**self.binding,field:value})
        other=self.store.enroll(**{**self.binding,'account_id':43,'manager_id':'manager_002'})
        self.assertNotEqual(other,self.thread)

    def test_partial_history_cannot_generate_or_send(self):
        with self.assertRaises(StaleDialogue):self.action(0)
        with self.assertRaises(ValueError):self.store.mark_history_complete(self.thread)

    def test_new_incoming_cancels_stale_answer_and_live_history_is_deduplicated(self):
        revision=self.history();action=self.action(revision)
        message=dict(telegram_id=11,direction='incoming',text='Question',sent_at=110,source='client')
        self.assertTrue(self.store.record_message(self.thread,**message))
        self.assertFalse(self.store.record_message(self.thread,**message))
        with self.assertRaises(ValueError):self.store.claim_action(action)
        with self.assertRaises(StaleDialogue):self.action(revision)
        self.assertEqual(len(self.store.snapshot(self.thread)[1]),2)

    def test_manual_reply_pauses_automation(self):
        self.history()
        self.store.record_message(self.thread,telegram_id=12,direction='outgoing',text='I will handle this',sent_at=120,source='manager')
        thread,_=self.store.snapshot(self.thread)
        self.assertEqual(thread['state'],'human')
        with self.assertRaises(ValueError):self.action(thread['revision'])

    def test_opt_out_stops_pending_work_and_allows_only_one_acknowledgement(self):
        revision=self.history();previous=self.action(revision)
        self.store.set_state(self.thread,'opt_out',expected_revision=revision)
        revision=self.store.snapshot(self.thread)[0]['revision']
        with self.assertRaises(ValueError):self.store.claim_action(previous)
        with self.assertRaises(ValueError):self.action(revision)
        ack=self.store.queue_action(self.thread,action_key='stop:11',kind='opt_out_ack',text='Извините, больше не напишем.',expected_revision=revision,due_at=999)
        self.store.claim_action(ack);self.store.finish_action(ack,status='sent',telegram_id=13)
        with self.assertRaises(ValueError):
            self.store.queue_action(self.thread,action_key='stop:12',kind='opt_out_ack',text='Again',expected_revision=revision,due_at=999)
        with self.assertRaises(ValueError):self.store.set_state(self.thread,'waiting',expected_revision=revision)

    def test_crash_or_timeout_never_replays_a_reserved_action(self):
        action=self.action(self.history());self.store.claim_action(action)
        reboot=DialogueStore(self.path,clock=lambda:1000)
        with self.assertRaises(ValueError):reboot.claim_action(action)
        reboot.finish_action(action,status='uncertain')
        with self.assertRaises(ValueError):reboot.claim_action(action)

    def test_sent_requires_real_telegram_id_and_cannot_be_completed_twice(self):
        action=self.action(self.history());self.store.claim_action(action)
        with self.assertRaises(ValueError):self.store.finish_action(action,status='sent')
        self.store.finish_action(action,status='sent',telegram_id=14)
        with self.assertRaises(ValueError):self.store.finish_action(action,status='sent',telegram_id=14)
