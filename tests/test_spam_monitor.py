from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from telethon.errors import FloodWaitError
from spam_monitor import SpamMonitor,parse_status

CLEAR='Good news, no limits are currently applied to your account. You’re free as a bird!'
LIMITED='Dear User,\nYour account is now limited until 24 Oct 2026, 15:11 UTC. Please wait.'


class SpamMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.flag=self.root/'send_spam_block.json'
        self.flag.write_text(json.dumps(dict(blocked=True,detected_at=100)))
        self.original=self.flag.read_bytes()
        self.now=datetime(2026,10,24,15,11,tzinfo=timezone.utc).timestamp()
        self.notify=AsyncMock()
        self.monitor=SpamMonitor(SimpleNamespace(),self.notify,self.root,lambda:self.now)

    def test_parser_uses_exact_clear_responses_and_reads_utc_deadline(self):
        self.assertEqual(parse_status(CLEAR),('clear',None))
        self.assertEqual(parse_status('Ваш аккаунт свободен от каких-либо ограничений.'),('clear',None))
        self.assertEqual(parse_status(LIMITED),('limited',self.now))
        self.assertEqual(parse_status('Other text quoting: '+CLEAR),('unknown',None))
        self.assertEqual(parse_status('Please choose a menu'),('unknown',None))

    async def test_repeats_with_pause_stops_on_clear_and_never_unblocks_sending(self):
        self.monitor.query=AsyncMock(side_effect=[parse_status(LIMITED),parse_status(CLEAR)])
        await self.monitor.step();await self.monitor.step()
        self.assertEqual(self.monitor.query.await_count,1)
        self.now+=60
        await self.monitor.step()
        self.assertEqual(self.monitor.query.await_count,2)
        self.assertEqual(self.flag.read_bytes(),self.original)
        self.assertIn('не отправляются',self.notify.call_args.args[0])
        reboot=SpamMonitor(SimpleNamespace(),self.notify,self.root,lambda:self.now+86400)
        reboot.query=AsyncMock()
        await reboot.step()
        reboot.query.assert_not_awaited()
        self.assertEqual(self.flag.read_bytes(),self.original)

    async def test_future_restriction_deadline_is_respected(self):
        self.now-=3600
        self.monitor.query=AsyncMock(return_value=parse_status(LIMITED))
        await self.monitor.step();self.now+=60;await self.monitor.step()
        self.assertEqual(self.monitor.query.await_count,1)
        state=json.loads(self.monitor.path.read_text('utf-8'))
        self.assertEqual(state['next_check'],parse_status(LIMITED)[1]+5)

    async def test_floodwait_does_not_trigger_a_rapid_retry(self):
        self.monitor.query=AsyncMock(side_effect=FloodWaitError(request=None,capture=1502))
        await self.monitor.step();self.now+=1500;await self.monitor.step()
        self.assertEqual(self.monitor.query.await_count,1)
        self.notify.assert_not_awaited()
        self.assertEqual(self.flag.read_bytes(),self.original)
        self.assertGreater(json.loads(self.monitor.path.read_text())['next_check'],self.now)

    async def test_notification_failure_retries_delivery_without_requerying_spambot(self):
        self.monitor.query=AsyncMock(return_value=('clear',None))
        self.notify.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError): await self.monitor.step()
        self.notify.side_effect=None
        await self.monitor.step()
        self.assertEqual(self.monitor.query.await_count,1)
        self.assertEqual(self.notify.await_count,2)

    async def test_no_request_without_incident_and_new_incident_gets_its_own_check(self):
        self.flag.unlink()
        self.monitor.query=AsyncMock(return_value=('clear',None))
        await self.monitor.step();self.monitor.query.assert_not_awaited()
        self.flag.write_text(json.dumps(dict(blocked=True,detected_at=101)))
        await self.monitor.step();self.assertEqual(self.monitor.query.await_count,1)
        self.flag.write_text(json.dumps(dict(blocked=True,detected_at=102)))
        await self.monitor.step();self.assertEqual(self.monitor.query.await_count,2)

    async def test_manual_reset_while_request_is_in_flight_does_not_publish_stale_notice(self):
        async def query():
            self.flag.unlink()
            return 'clear',None
        self.monitor.query=query
        await self.monitor.step();self.notify.assert_not_awaited()

    async def test_transport_only_sends_start_to_official_spambot_and_reuses_peer(self):
        conversation=SimpleNamespace(send_message=AsyncMock(return_value='own-request'),
                                     get_response=AsyncMock(return_value=SimpleNamespace(raw_text=CLEAR)))
        class Context:
            async def __aenter__(self): return conversation
            async def __aexit__(self,*args): return False
        from unittest.mock import Mock
        client=SimpleNamespace(get_input_entity=AsyncMock(return_value='official-peer'),
                               conversation=Mock(return_value=Context()))
        self.monitor.client=client
        self.assertEqual(await self.monitor.query(),('clear',None))
        self.assertEqual(await self.monitor.query(),('clear',None))
        client.get_input_entity.assert_awaited_once_with('SpamBot')
        self.assertTrue(all(c.args[0]=='official-peer' for c in client.conversation.call_args_list))
        self.assertTrue(all(c.args==('/start',) for c in conversation.send_message.call_args_list))
        conversation.get_response.assert_awaited_with('own-request')
