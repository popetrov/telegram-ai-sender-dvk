import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from telethon.sessions import MemorySession
from telethon.tl import functions,types
from safe_telegram import ProtectedTelegramClient,TelegramDeletionBlocked,safe_targets,ephemeral_media
from telegram_safety import SafetyJournal
import incident_monitor as m

class CauseProtection(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.r=m.Recorder(self.root/'monitor.sqlite')
  self.c=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=SafetyJournal(self.root/'safety.sqlite'))
 def events(self):
  db=self.r.connect()
  try:return [json.loads(r[0]) for r in db.execute('SELECT data FROM monitor_events')]
  finally:db.close()
 async def test_ephemeral_file_rejected_at_final_dispatch_without_network(self):
  media=types.InputMediaUploadedPhoto(types.InputFile(1,1,'secret.jpg','x'),ttl_seconds=5)
  req=functions.messages.SendMediaRequest(types.InputPeerUser(77,0),media,'secret caption')
  sender=SimpleNamespace(send=Mock());original=sender.send;self.c._guard_sender(sender)
  with patch.object(m,'_default',self.r):
   with self.assertRaises(TelegramDeletionBlocked):sender.send(req)
  original.assert_not_called()
  event=next(r for r in self.events() if r['kind']=='request_blocked');self.assertEqual(event['operation'],'ephemeral_media_ttl');self.assertEqual(event['peer_id'],77)
  self.assertNotIn('secret',json.dumps(event))
 async def test_batch_and_wrapper_media_timer_detected(self):
  req=functions.messages.SendMediaRequest(types.InputPeerUser(77,0),types.InputMediaPhoto(types.InputPhoto(1,2,b''),ttl_seconds=1),'x')
  wrapped=functions.InvokeWithoutUpdatesRequest(req)
  self.assertTrue(ephemeral_media([functions.updates.GetStateRequest(),wrapped]))
  with patch.object(m,'_default',self.r):
   with self.assertRaises(TelegramDeletionBlocked):self.c._check_request([wrapped])
 async def test_regular_media_and_cycles_are_safe(self):
  self.assertFalse(ephemeral_media(types.InputMediaPhoto(types.InputPhoto(1,2,b''))))
  cyclic=SimpleNamespace();cyclic.media=cyclic
  self.assertFalse(ephemeral_media(cyclic))
 async def test_blocked_delete_preserves_ids_and_peer_without_text(self):
  req=functions.messages.DeleteHistoryRequest(types.InputPeerUser(77,0),max_id=0)
  with patch.object(m,'_default',self.r):
   with self.assertRaises(TelegramDeletionBlocked):self.c._check_request(req)
  self.assertEqual(next(r for r in self.events() if r['kind']=='request_blocked')['peer_id'],77)
  self.assertEqual(safe_targets(functions.InvokeWithoutUpdatesRequest(functions.messages.DeleteMessagesRequest([9,10],revoke=True)))['message_ids'],[9,10])
 async def test_disk_failure_cannot_allow_ephemeral_send(self):
  with patch.object(m,'record',side_effect=OSError()):
   with self.assertRaises(TelegramDeletionBlocked):self.c._check_request(SimpleNamespace(ttl_seconds=1))
 async def test_ttl_enabled_recorded_and_warned_once(self):
  client=AsyncMock(return_value=SimpleNamespace(full_user=SimpleNamespace(ttl_period=86400)))
  notify=AsyncMock()
  for _ in range(2):self.assertEqual(await m.capture_ttl(client,77,notify,self.r),86400)
  notify.assert_awaited_once();self.assertEqual(self.events()[0]['count'],86400)
  self.assertTrue(all(isinstance(c.args[0],functions.users.GetFullUserRequest) for c in client.await_args_list))
 async def test_ttl_warning_failure_retried(self):
  client=AsyncMock(return_value=SimpleNamespace(period=86400));notify=AsyncMock(side_effect=[OSError(),None])
  with self.assertRaises(OSError):await m.capture_ttl(client,notify=notify,recorder=self.r)
  await m.capture_ttl(client,notify=notify,recorder=self.r)
  self.assertEqual(notify.await_count,2)
 async def test_absent_chat_timer_off_does_not_warn(self):
  client=AsyncMock(return_value=SimpleNamespace(full_user=SimpleNamespace()));notify=AsyncMock()
  self.assertEqual(await m.capture_ttl(client,77,notify,self.r),0);notify.assert_not_awaited()
 async def test_invalid_timer_is_not_silently_off(self):
  client=AsyncMock(return_value=SimpleNamespace(period=None))
  with self.assertRaises(ValueError):await m.capture_ttl(client,recorder=self.r)
 async def test_center_auto_delete_notice_deduplicates_and_uses_trusted_manager(self):
  store=SimpleNamespace();notices=SimpleNamespace(once=Mock())
  row=dict(kind='monitor_warning',reason_code='ttl_snapshot',source='chat_history_ttl',count=86400,peer_id=77)
  with patch('owner_notifications.manager_label',return_value='Synthetic manager'):
   m.center_notice(store,notices,42,'a'*32,{'data':{'table':'monitor_events','row':row}})
  self.assertIn('Synthetic manager',notices.once.call_args.args[1])
  self.assertIn('86400',notices.once.call_args.args[1])
  self.assertTrue(notices.once.call_args.args[0].startswith('monitor-ttl:42:77:86400:'))

if __name__=='__main__':unittest.main()
