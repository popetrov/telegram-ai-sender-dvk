from pathlib import Path
import tempfile
import unittest
from dialogue_store import DialogueStore,StaleDialogue


class DialogueStoreTests(unittest.TestCase):
    def failed_reminder(self):
        revision=self.history()
        self.store.handoff(self.thread,event_key='knowledge_missing:reminder:1',event_type='knowledge_missing',
            summary='Could not prepare reminder',pause=True,expected_revision=revision)

    def test_recover_only_untouched_reminder_error_and_preserve_audit(self):
        self.failed_reminder()
        self.assertEqual(self.store.recover_failed_reminders(account_id=42,manager_id='manager_001',dry_run=True),[self.thread])
        self.assertEqual(self.store.thread(self.thread)['state'],'human')
        self.assertEqual(self.store.recover_failed_reminders(account_id=42,manager_id='manager_001'),[self.thread])
        self.assertEqual(self.store.thread(self.thread)['state'],'waiting')
        self.assertEqual(self.store.recover_failed_reminders(account_id=42,manager_id='manager_001'),[])

    def test_manual_pause_or_client_message_is_not_recovered(self):
        self.failed_reminder()
        thread=self.store.thread(self.thread)
        self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='pause',expected_revision=thread['revision'])
        self.assertEqual(self.store.recover_failed_reminders(account_id=42,manager_id='manager_001'),[])

    def test_optout_and_foreign_manager_are_not_recovered(self):
        self.failed_reminder()
        self.assertEqual(self.store.recover_failed_reminders(account_id=43,manager_id='manager_001'),[])
        thread=self.store.thread(self.thread)
        self.store.set_state(self.thread,'opt_out',expected_revision=thread['revision'])
        self.assertEqual(self.store.recover_failed_reminders(account_id=42,manager_id='manager_001'),[])

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

    def test_ready_handoff_pauses_atomically_and_survives_restart_without_duplicate_notice(self):
        revision=self.history();action=self.action(revision)
        values=dict(event_key='ready:11',event_type='ready',summary='Ready to apply',pause=True,expected_revision=revision)
        notice=self.store.handoff(self.thread,**values)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'human')
        with self.assertRaises(ValueError):self.store.claim_action(action)
        reboot=DialogueStore(self.path,clock=lambda:1000)
        self.assertEqual(reboot.handoff(self.thread,**values),notice)
        self.assertEqual(len(reboot.pending_notices(42)),1)
        self.assertEqual(reboot.pending_notices(43),[])
        with self.assertRaises(ValueError):reboot.acknowledge_notice(notice,account_id=43)
        reboot.acknowledge_notice(notice,account_id=42)
        self.assertEqual(reboot.pending_notices(42),[])

    def test_interest_notice_does_not_pause_and_manager_can_take_and_return(self):
        revision=self.history()
        self.store.handoff(self.thread,event_key='need:11',event_type='need',summary='Guarantee required',pause=False,expected_revision=revision)
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'waiting')
        with self.assertRaises(PermissionError):
            self.store.manager_control(self.thread,actor_account_id=43,manager_id='manager_001',command='pause',expected_revision=revision)
        self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='pause',expected_revision=revision)
        thread,_=self.store.snapshot(self.thread)
        self.assertEqual(thread['state'],'human')
        with self.assertRaises(StaleDialogue):
            self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='resume',expected_revision=revision)
        self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='resume',expected_revision=thread['revision'])
        self.assertEqual(self.store.snapshot(self.thread)[0]['state'],'engaged')

    def test_manager_resume_cannot_override_client_opt_out(self):
        revision=self.history();self.store.set_state(self.thread,'opt_out',expected_revision=revision)
        revision=self.store.snapshot(self.thread)[0]['revision']
        with self.assertRaises(ValueError):
            self.store.manager_control(self.thread,actor_account_id=42,manager_id='manager_001',command='resume',expected_revision=revision)

    def test_followup_schedule_counts_only_confirmed_deliveries(self):
        revision=self.history()
        for stage,status in ((1,'sent'),(2,'uncertain')):
            action=self.store.queue_action(self.thread,action_key=f'reminder:{stage}',kind='reminder',text='Follow up',expected_revision=revision,due_at=999)
            self.store.claim_action(action)
            self.store.finish_action(action,status=status,telegram_id=15 if status=='sent' else None)
        self.assertEqual(self.store.reminder_history(self.thread),[1000])
