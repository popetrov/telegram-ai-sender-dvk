import unittest,tempfile,json,sqlite3,urllib.error
from pathlib import Path
from unittest.mock import Mock,patch
from incident_monitor import Recorder,center_notice
from startup_update import update_failure_report
from telegram_safety import notice_text
class MonitorTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.p=Path(self.t.name);self.r=Recorder(self.p/'journal.sqlite')
 def test_durable_priority_and_ack(self):
  first=self.r.record('startup'); urgent=self.r.record('telegram_deleted',message_ids=[12],source='tracked_private_history')
  other=Recorder(self.r.path);self.assertEqual(other.pending()['event_id'],urgent)
  other.acknowledge(urgent);self.assertEqual(other.pending()['event_id'],first)
 def test_secrets_and_exception_text_rejected(self):
  with self.assertRaises(ValueError):self.r.record('send_error',token='secret')
  with self.assertRaises(ValueError):self.r.record('send_error',error_type='url https://secret')
 def test_only_two_successful_misses_after_observation_alert(self):
  self.r.observe([5]);self.assertEqual(self.r.apply_check([5],set()),[])
  self.assertEqual(self.r.apply_check([5],{5}),[])
  self.assertEqual(self.r.apply_check([5],set()),[])
  self.assertEqual(self.r.apply_check([5],set()),[5])
  self.assertEqual(self.r.apply_check([5],set()),[])
 def test_initial_missing_is_not_new_deletion(self):
  db=self.p/'local.sqlite'
  with sqlite3.connect(db) as c:c.execute('CREATE TABLE messages(telegram_message_id INTEGER,status TEXT)');c.execute("INSERT INTO messages VALUES (11,'sent')")
  c.close();self.r.seed(db);self.assertEqual(self.r.apply_check([11],set()),[]);self.assertEqual(self.r.apply_check([11],set()),[])
  self.assertEqual(json.loads(self.r.pending()['data'])['kind'],'baseline_missing')
 def test_event_does_not_claim_prevention(self):
  self.assertIn('пока неизвестно',notice_text({'operation':'observed_telegram_deletion'}))
  self.assertIn('не подтверждение',notice_text({'operation':'observed_monitor_unavailable'}))
 def test_update_report_omits_exception_secret(self):
  (self.p/'version.txt').write_text('2.0.32')
  report=update_failure_report(self.p,self.p,urllib.error.URLError('SECRET_TOKEN'))
  self.assertNotIn('SECRET_TOKEN',report.read_text());self.assertEqual(json.loads(report.read_text())['error_type'],'URLError')
 def test_unknown_deletion_does_not_spam_owner(self):
  notices=Mock();center_notice(Mock(),notices,1,'id',{'data':{'table':'monitor_events','row':{'kind':'telegram_deleted','source':'unattributed_update'}}});notices.once.assert_not_called()
class TransportTests(unittest.IsolatedAsyncioTestCase):
 async def test_encrypted_upload_acknowledged_only_after_signed_receipt(self):
  from types import SimpleNamespace
  from unittest.mock import AsyncMock
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
  import base64
  import incident_monitor as m
  from sync_protocol import initialize_encryption_key,public_text,decrypt,receipt,event_digest
  with tempfile.TemporaryDirectory() as temp:
   r=Recorder(Path(temp)/'monitor.sqlite');eid=r.record('startup')
   enc=initialize_encryption_key(Path(temp)/'enc.key');sign=Ed25519PrivateKey.generate()
   pub=base64.b64encode(sign.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
   conversation=AsyncMock();conversation.__aenter__.return_value=conversation
   client=Mock();client.get_me=AsyncMock(return_value=SimpleNamespace(id=42))
   client.get_entity=AsyncMock(return_value=SimpleNamespace(id=7,bot=True,username='service_bot'))
   client.conversation.return_value=conversation
   async def send(peer,stream,**kwargs):
    envelope=decrypt(enc,stream.getvalue());self.assertEqual(envelope['subject'],'manager_001:42')
    ack=receipt(sign,envelope['subject'],eid,event_digest(envelope['data']))
    conversation.get_response.return_value=SimpleNamespace(raw_text='DVK_RECEIPT '+json.dumps(ack))
    return SimpleNamespace(id=8)
   client.send_file=AsyncMock(side_effect=send)
   settings={'ACCESS_BOT_USERNAME':'service_bot','MANAGER_ID':'manager_001','SYNC_PUBLIC_KEY':public_text(enc),'ACCESS_PUBLIC_KEY':pub}
   with patch.object(m,'_default',r),patch.dict(m.os.environ,settings):
    self.assertTrue(await m.flush(client));self.assertIsNone(r.pending())
    r.record('send_error',error_type='TimeoutError');client.send_file.side_effect=TimeoutError
    with self.assertRaises(TimeoutError):await m.flush(client)
    self.assertIsNotNone(r.pending())
if __name__=='__main__':unittest.main()


