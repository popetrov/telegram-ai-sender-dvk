"""End-to-end encrypted telemetry, authenticated storage and safety checks."""
import asyncio,base64,json,logging,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from cryptography.exceptions import InvalidSignature
from telethon.sessions import MemorySession
from telethon import TelegramClient
from telethon.tl import functions,types
from authority_store import AuthorityStore
from owner_notifications import OwnerNotifications
from sync_protocol import initialize_encryption_key,public_text,decrypt,receipt,event_digest
from telegram_safety import SafetyJournal,SafetyStop
from safe_telegram import ProtectedTelegramClient
import incident_monitor as m

class MonitoringIntegration(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.recorder=m.Recorder(self.root/'monitor.sqlite')
  self.store=AuthorityStore(self.root/'center.sqlite');self.store.register(42,'manager_001:42');self.store.assign(42,'dvk')
  self.notices=OwnerNotifications(self.store)
  self.encryption=initialize_encryption_key(self.root/'encryption.key');self.sign=Ed25519PrivateKey.generate()
  self.public=base64.b64encode(self.sign.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
  self.settings={'ACCESS_BOT_USERNAME':'service_bot','MANAGER_ID':'manager_001','SYNC_PUBLIC_KEY':public_text(self.encryption),'ACCESS_PUBLIC_KEY':self.public}
  self.conversation=AsyncMock();self.conversation.__aenter__.return_value=self.conversation
  self.client=Mock();self.client.get_me=AsyncMock(return_value=SimpleNamespace(id=42))
  self.client.get_entity=AsyncMock(return_value=SimpleNamespace(id=7,bot=True,username='service_bot'))
  self.client.conversation.return_value=self.conversation
  self.packets=[]
  async def deliver(peer,stream,**kw):
   packet=decrypt(self.encryption,stream.getvalue());self.packets.append(packet)
   self.store.receive(42,packet['subject'],packet['event_id'],event_digest(packet['data']),packet['data'])
   m.center_notice(self.store,self.notices,42,packet['event_id'],packet['data'])
   ack=receipt(self.sign,packet['subject'],packet['event_id'],event_digest(packet['data']))
   self.conversation.get_response.return_value=SimpleNamespace(raw_text='DVK_RECEIPT '+json.dumps(ack))
   return SimpleNamespace(id=99)
  self.client.send_file=AsyncMock(side_effect=deliver)
 def rows(self):
  db=self.recorder.connect()
  try:return [json.loads(r[0]) for r in db.execute('SELECT data FROM monitor_events ORDER BY sequence')]
  finally:db.close()
 async def test_batch_encrypted_center_export_and_duplicate_delivery(self):
  self.recorder.bind(42,'manager_001')
  for i in range(8):self.recorder.record('send_result',message_ids=[100+i],request_id='a'*32)
  urgent=self.recorder.record('telegram_deleted',message_ids=[100],source='tracked_private_history')
  with patch.object(m,'_default',self.recorder),patch.dict(m.os.environ,self.settings):self.assertTrue(await m.flush(self.client))
  self.assertIsNone(self.recorder.pending());self.client.send_file.assert_awaited_once()
  report=m.export_report(self.store,42);self.assertEqual(len(report['events']),9)
  self.assertIn(urgent,[e['event_id'] for e in report['events']])
  packet=self.packets[0]
  self.store.receive(42,packet['subject'],packet['event_id'],event_digest(packet['data']),packet['data'])
  m.center_notice(self.store,self.notices,42,packet['event_id'],packet['data'])
  self.assertEqual(len(m.export_report(self.store,42)['events']),9)
  with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_notices').fetchone()[0],1)
 async def test_invalid_receipt_retains_pending_and_does_not_ack(self):
  self.recorder.record('heartbeat')
  async def send(*a,**k):
   self.conversation.get_response.return_value=SimpleNamespace(raw_text='DVK_RECEIPT '+json.dumps({'payload':{'event_id':self.recorder.pending()['event_id']},'signature':base64.b64encode(b'x'*64).decode()}))
   return SimpleNamespace(id=2)
  self.client.send_file.side_effect=send
  with patch.object(m,'_default',self.recorder),patch.dict(m.os.environ,self.settings):
   with self.assertRaises((InvalidSignature,ValueError)):await m.flush(self.client)
  self.assertIsNotNone(self.recorder.pending())
 async def test_unknown_or_changed_identity_is_rejected_without_storage(self):
  self.recorder.record('startup',account_id=999,manager_id='manager_001')
  row=json.loads(self.recorder.pending()['data']);data={'installation':'install','data':{'table':'monitor_events','row':row}}
  with self.assertRaises(ValueError):self.store.receive(42,'manager_001:42','b'*32,event_digest(data),data)
  with self.assertRaises(ValueError):self.store.receive(42,'manager_wrong:42','c'*32,event_digest(data),data)
  self.assertEqual(m.export_report(self.store,42)['events'],[])
 async def test_bind_cannot_mix_accounts(self):
  self.recorder.bind(42,'manager_001');self.recorder.bind(42,'manager_001')
  with self.assertRaises(ValueError):self.recorder.bind(43,'manager_001')
 async def test_program_send_has_pre_attempt_and_returned_message_id(self):
  client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=SafetyJournal(self.root/'safety.sqlite'))
  response=SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=12345,out=True))])
  with patch.object(m,'_default',self.recorder),patch.object(TelegramClient,'_call',new=AsyncMock(return_value=response)):
   await client._call(Mock(),functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'PRIVATE MESSAGE'))
  events=self.rows();self.assertEqual([e['kind'] for e in events],['send_attempt','send_result'])
  self.assertEqual(events[1]['message_ids'],[12345]);self.assertEqual(events[1]['request_id'],events[0].get('request_id',events[1]['request_id']))
  self.assertNotIn('PRIVATE MESSAGE',json.dumps(events))
 async def test_error_and_cancellation_are_recorded_without_message_text(self):
  client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=SafetyJournal(self.root/'safety.sqlite'))
  with patch.object(m,'_default',self.recorder),patch.object(TelegramClient,'_call',new=AsyncMock(side_effect=RuntimeError('SECRET_TOKEN https://private'))):
   with self.assertRaises(RuntimeError):await client._call(Mock(),functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'PRIVATE MESSAGE'))
  events=self.rows();self.assertEqual(events[-1]['kind'],'send_error');self.assertEqual(events[-1]['error_type'],'RuntimeError')
  self.assertNotIn('SECRET_TOKEN',json.dumps(events));self.assertNotIn('PRIVATE MESSAGE',json.dumps(events))
 async def test_broken_journal_latches_sending(self):
  journal=SafetyJournal(self.root/'safety.sqlite')
  with patch.object(m,'_default',self.recorder),patch.object(self.recorder,'record',side_effect=sqlite3.OperationalError),patch('telegram_safety.default_journal',return_value=journal):
   with self.assertRaises(SafetyStop):m.record('send_attempt')
  self.assertEqual(journal.current()['operation'],'observed_monitor_unavailable')
 async def test_logging_redacts_raw_text_and_throttles(self):
  record=logging.LogRecord('telethon.network',logging.ERROR,'secret/path',1,'TOKEN SECRET_CLIENT_MESSAGE',(),None)
  with patch.object(m,'_default',self.recorder):
   handler=m.ErrorRecorder();handler.emit(record);handler.emit(record)
  self.assertEqual(len(self.rows()),1);self.assertNotIn('TOKEN',json.dumps(self.rows()));self.assertNotIn('SECRET_CLIENT_MESSAGE',json.dumps(self.rows()))
 async def test_admin_and_callback_reject_other_accounts(self):
  app=Mock();m.register_admin(app,self.store,1)
  commands=[c.args[0] for c in app.add_handler.call_args_list]
  update=SimpleNamespace(effective_user=SimpleNamespace(id=2),effective_chat=SimpleNamespace(type='private'),effective_message=SimpleNamespace(reply_text=AsyncMock()),callback_query=SimpleNamespace(answer=AsyncMock()))
  for handler in commands:await handler.callback(update,SimpleNamespace(args=['42']))
  update.effective_message.reply_text.assert_not_awaited();update.callback_query.answer.assert_not_awaited()
 async def test_admin_exports_document_and_no_raw_credentials(self):
  row={'kind':'heartbeat','run_id':'f'*32,'recorded_at':123}
  data={'installation':'install','data':{'table':'monitor_events','row':row}}
  self.store.receive(42,'manager_001:42','c'*32,event_digest(data),data)
  app=Mock();m.register_admin(app,self.store,1)
  handler=next(c.args[0] for c in app.add_handler.call_args_list if getattr(c.args[0],'commands',set())==frozenset({'monitor_export'}))
  message=SimpleNamespace(text='/monitor_export 42',reply_document=AsyncMock(),reply_text=AsyncMock())
  update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),effective_message=message)
  await handler.callback(update,SimpleNamespace(args=['42']))
  doc=message.reply_document.await_args.kwargs['document'];report=json.loads(doc.getvalue())
  self.assertEqual(report['account_id'],42);self.assertEqual(len(report['events']),1)

 async def test_observed_private_deletion_isolates_and_notifies_once(self):
  class Dummy:
   def __init__(self):self.handlers=[];self.get_messages=AsyncMock(return_value=[SimpleNamespace(id=321,message='not exported')])
   def add_event_handler(self,callback,event):self.handlers.append(callback)
   def remove_event_handler(self,callback):pass
   def is_connected(self):return True
   async def __call__(self,request):return SimpleNamespace(authorizations=[])
  client=Dummy();notify=AsyncMock();journal=SafetyJournal(self.root/'observed-safety.sqlite')
  self.recorder.observe([321],77)
  with patch.object(m,'ROOT',self.root),patch.object(m,'_default',self.recorder),patch.object(m,'flush',new=AsyncMock(return_value=False)),patch('telegram_safety.default_journal',return_value=journal):
   task=asyncio.create_task(m.run(client,notify))
   try:
    await asyncio.sleep(0.05)
    self.assertEqual(len(client.handlers),2)
    event=SimpleNamespace(is_channel=False,deleted_ids=[321])
    await client.handlers[0](event);await client.handlers[0](event)
    self.assertIsNone(journal.current());notify.assert_awaited_once()
    from incident_isolation import require_peer_allowed,PeerQuarantined
    with self.assertRaises(PeerQuarantined):require_peer_allowed(77,self.recorder)
    require_peer_allowed(88,self.recorder);journal.require_clear()
   finally:
    task.cancel()
    try:await task
    except asyncio.CancelledError:pass
 async def test_network_failure_and_unknown_ids_do_not_claim_deletion(self):
  class Dummy:
   def __init__(self):self.handlers=[];self.get_messages=AsyncMock(side_effect=TimeoutError)
   def add_event_handler(self,callback,event):self.handlers.append(callback)
   def remove_event_handler(self,callback):pass
   def is_connected(self):return True
   async def __call__(self,request):return SimpleNamespace(authorizations=[])
  client=Dummy();notify=AsyncMock();journal=SafetyJournal(self.root/'network-safety.sqlite')
  self.recorder.observe([321],77)
  with patch.object(m,'ROOT',self.root),patch.object(m,'_default',self.recorder),patch.object(m,'flush',new=AsyncMock(return_value=False)),patch('telegram_safety.default_journal',return_value=journal):
   task=asyncio.create_task(m.run(client,notify))
   try:
    await asyncio.sleep(0.05)
    await client.handlers[0](SimpleNamespace(is_channel=False,deleted_ids=[999]))
    await client.handlers[0](SimpleNamespace(is_channel=True,deleted_ids=[321]))
    self.assertIsNone(journal.current());notify.assert_not_awaited()
    self.assertTrue(any(r['kind']=='runtime_error' and r.get('stage')=='history_check' for r in self.rows()))
   finally:
    task.cancel()
    try:await task
    except asyncio.CancelledError:pass

 async def test_technical_lease_does_not_become_customer_history(self):
  client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=SafetyJournal(self.root/'protocol-safety.sqlite'))
  response=SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=54321,out=True))])
  with patch.object(m,'_default',self.recorder),patch.object(TelegramClient,'_call',new=AsyncMock(return_value=response)):
   await client._call(Mock(),functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'/lease protocol_payload'))
  self.assertEqual(self.rows(),[])

 async def test_critical_runtime_error_notifies_once_but_network_warning_does_not(self):
  data={'installation':'install','data':{'table':'monitor_events','row':{'kind':'runtime_error','error_type':'RuntimeError','level':40,'logger':'dialogue_runtime'}}}
  m.center_notice(self.store,self.notices,42,'first',data);m.center_notice(self.store,self.notices,42,'second',data)
  data['data']['row']['level']=30;data['data']['row']['error_type']='TimeoutError'
  m.center_notice(self.store,self.notices,42,'network',data)
  with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_notices').fetchone()[0],1)

if __name__=='__main__':unittest.main()
