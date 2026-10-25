from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo
from dialogue_policy import DialoguePolicy

ROOT=Path(__file__).resolve().parents[1]


class FinancePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy=DialoguePolicy.load(ROOT/'dialogue_profiles/dvk_finance.json')

    def at(self,value):
        return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Dubai')).timestamp())

    def test_intervals_start_at_each_actual_send_then_stop_after_three(self):
        first=self.at('2026-10-24T10:00:00')
        one=self.policy.next_reminder_at(first,[])
        self.assertEqual(one,self.at('2026-10-27T10:00:00'))
        two=self.policy.next_reminder_at(first,[one])
        self.assertEqual(two,self.at('2026-11-27T10:00:00'))
        three=self.policy.next_reminder_at(first,[one,two])
        self.assertEqual(three,self.at('2027-05-27T10:00:00'))
        self.assertIsNone(self.policy.next_reminder_at(first,[one,two,three]))

    def test_delayed_first_reminder_does_not_catch_up_month_immediately(self):
        first=self.at('2026-10-24T10:00:00')
        actual=self.at('2027-01-31T10:00:00')
        self.assertEqual(self.policy.next_reminder_at(first,[actual]),self.at('2027-02-28T10:00:00'))
        first=self.at('2027-10-24T10:00:00');actual=self.at('2028-01-31T10:00:00')
        self.assertEqual(self.policy.next_reminder_at(first,[actual]),self.at('2028-02-29T10:00:00'))

    def test_reply_stops_silent_schedule_and_invalid_history_rejected(self):
        self.assertIsNone(self.policy.next_reminder_at(100,[],has_reply=True))
        for times in ([90],[100],[200,190],[200,300,400,500]):
            with self.assertRaises(ValueError):self.policy.next_reminder_at(100,times)

    def test_manager_handoff_rules_and_product_scope_match_owner_decisions(self):
        self.assertEqual(set(self.policy.products),{'БГ','Кредит'})
        for event in ('ready','knowledge_missing'):
            self.assertTrue(self.policy.handoff(event).pause)
            self.assertTrue(self.policy.handoff(event).notify)
        for event in ('need','call','documents'):
            self.assertFalse(self.policy.handoff(event).pause)
            self.assertTrue(self.policy.handoff(event).notify)
        self.assertFalse(self.policy.enabled)
