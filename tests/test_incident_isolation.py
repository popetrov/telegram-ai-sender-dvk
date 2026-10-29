import asyncio,contextlib,importlib.util,json,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,Mock,patch
H=Path(__file__).resolve().parents[1];sys.path.insert(0,str(H));import incident_monitor as m
from incident_isolation import isolate,require_peer_allowed,notify_pending,PeerQuarantined
from dialogue_store import DialogueStore,require_contact_allowed,ContactOptOutError
from telegram_safety import SafetyJournal,SafetyStop
from safe_telegram import ProtectedTelegramClient,TelegramDeletionBlocked
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import functions,types
from authority_store import AuthorityStore
from owner_notifications import OwnerNotifications
class Isolation(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=H);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.db=self.root/'local.sqlite';self.s=DialogueStore(self.db);self.s.initialize();self.r=m.Recorder(self.root/'monitor.sqlite');self.r.bind(42,'manager_1');self.r.observe([100],77);self.r.observe([200],88);self.j=SafetyJournal(self.root/'safety.sqlite')
  self.tids=[]
  for peer,mid in [(77,100),(88,200)]:self.tids.append(self.s.enroll(company_id='companyA',business_id='finance',manager_id='manager_1',account_id=42,peer_id=peer,product='Кредит',first_message_id=mid,first_sent_at=int(time.time())))
 def missing(self,ids=[100]):return isolate(self.r,ids,self.db,'telegram_deleted')
 async def test_only_target_blocked_other_chat_can_send(self):
  result=self.missing();self.assertEqual(result['peers'],[77]);self.assertFalse(result['unresolved']);self.j.require_clear()
  with self.assertRaises(PeerQuarantined):require_peer_allowed(77,self.r)
  require_peer_allowed(88,self.r);self.assertEqual(self.s.thread(self.tids[0])['state'],'closed');self.assertEqual(self.s.thread(self.tids[1])['state'],'waiting')
 async def test_persists_on_restart(self):
  self.missing();again=m.Recorder(self.r.path)
  with self.assertRaises(PeerQuarantined):require_peer_allowed(77,again)
 async def test_repeat_and_history_check_one_notification(self):
  self.missing();self.missing();isolate(self.r,[100],self.db,'history_missing');notify=AsyncMock();await notify_pending(self.r,notify);await notify_pending(self.r,notify);notify.assert_awaited_once()
 async def test_notification_retries_without_unblocking(self):
  self.missing();notify=AsyncMock(side_effect=[TimeoutError,None]);await notify_pending(self.r,notify);await notify_pending(self.r,notify);self.assertEqual(notify.await_count,2)
  with self.assertRaises(PeerQuarantined):require_peer_allowed(77,self.r)
 async def test_transport_stops_target_before_network_but_other_sends(self):
  self.missing();client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=self.j)
  with patch.object(m,'_default',self.r),patch.object(TelegramClient,'_call',new_callable=AsyncMock,return_value='ok') as network:
   with self.assertRaises(PeerQuarantined):await client(functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'private'))
   network.assert_not_awaited();self.assertEqual(await client(functions.messages.SendMessageRequest(types.InputPeerUser(88,0),'other')),'ok')
 async def test_preparation_guard_uses_same_persistent_block(self):
  self.missing()
  with patch.object(m,'_default',self.r):
   with self.assertRaises(ContactOptOutError):require_contact_allowed(self.db,manager_id='manager_1',peer_id=77)
   require_contact_allowed(self.db,manager_id='manager_1',peer_id=88)
 async def test_unknown_peer_stays_unresolved_not_guessed(self):
  self.r.observe([300]);result=self.missing([300]);self.assertEqual(result['unresolved'],[300]);self.assertEqual(result['peers'],[])
 async def test_old_seed_resolves_from_own_primary_message(self):
  with self.s.connection() as db:
   db.executescript('CREATE TABLE clients(id INTEGER PRIMARY KEY,telegram_id INTEGER,manager_id TEXT);CREATE TABLE messages(telegram_message_id INTEGER,client_id INTEGER);')
   db.execute('INSERT INTO clients VALUES(1,99,?)',('manager_1',));db.execute('INSERT INTO messages VALUES(300,1)')
  self.r.observe([300]);self.assertEqual(self.missing([300])['peers'],[99])
 async def test_other_manager_primary_not_used(self):
  with self.s.connection() as db:
   db.executescript('CREATE TABLE clients(id INTEGER PRIMARY KEY,telegram_id INTEGER,manager_id TEXT);CREATE TABLE messages(telegram_message_id INTEGER,client_id INTEGER);')
   db.execute('INSERT INTO clients VALUES(1,99,?)',('manager_OTHER',));db.execute('INSERT INTO messages VALUES(300,1)')
  self.r.observe([300]);self.assertEqual(self.missing([300])['unresolved'],[300])
 async def test_dangerous_program_request_still_stops_all(self):
  client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=self.j)
  with patch.object(m,'_default',self.r),patch.object(TelegramClient,'_call',new_callable=AsyncMock) as network:
   with self.assertRaises(TelegramDeletionBlocked):await client(functions.messages.DeleteMessagesRequest([100],revoke=True))
   network.assert_not_awaited()
   with self.assertRaises(SafetyStop):await client(functions.messages.SendMessageRequest(types.InputPeerUser(88,0),'other'))
 async def test_corrupt_quarantine_state_fails_closed(self):
  self.r.path.write_bytes(b'broken');client=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=self.j)
  with patch.object(m,'_default',self.r),patch.object(TelegramClient,'_call',new_callable=AsyncMock) as network:
   with self.assertRaises(SafetyStop):await client(functions.messages.SendMessageRequest(types.InputPeerUser(88,0),'other'))
   network.assert_not_awaited()
 async def test_optout_remains_optout(self):
  self.s.set_state(self.tids[0],'opt_out',expected_revision=self.s.thread(self.tids[0])['revision']);self.missing();self.assertEqual(self.s.thread(self.tids[0])['state'],'opt_out')
 async def test_business_tables_preserved(self):
  with self.s.connection() as db:before=[tuple(r) for r in db.execute('SELECT * FROM dialogue_messages')]
  self.missing()
  with self.s.connection() as db:self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM dialogue_messages')])
 async def test_upgrade_archives_observed_pause_after_blocking_peer(self):
  from incident_isolation import migrate_observed_pause
  self.j.block('observed_telegram_deletion');self.r.record('telegram_deleted',message_ids=[100],source='tracked_private_history')
  result=migrate_observed_pause(self.r,self.db,self.j);self.assertIsNotNone(result);self.assertIsNone(self.j.current())
  self.assertTrue((self.j.path.parent/'safety-archive'/(result['incident']['incident_id']+'.sqlite')).exists())
  with self.assertRaises(PeerQuarantined):require_peer_allowed(77,self.r)
  require_peer_allowed(88,self.r)
 async def test_upgrade_does_not_clear_dangerous_or_unresolved_stop(self):
  from incident_isolation import migrate_observed_pause
  self.j.block('DeleteMessagesRequest');self.r.record('telegram_deleted',message_ids=[100],source='tracked_private_history')
  self.assertIsNone(migrate_observed_pause(self.r,self.db,self.j));self.assertEqual(self.j.current()['operation'],'DeleteMessagesRequest')
 async def test_upgrade_unknown_id_keeps_global_pause(self):
  from incident_isolation import migrate_observed_pause
  self.j.block('observed_telegram_deletion');self.r.record('telegram_deleted',message_ids=[999],source='tracked_private_history')
  self.assertIsNone(migrate_observed_pause(self.r,self.db,self.j));self.assertTrue(self.j.path.exists())
 async def test_upgrade_does_not_clear_later_dangerous_attempt(self):
  from incident_isolation import migrate_observed_pause
  self.j.block('observed_telegram_deletion');self.r.record('telegram_deleted',message_ids=[100],source='tracked_private_history');self.r.record('request_blocked',operation='DeleteMessagesRequest')
  self.assertIsNone(migrate_observed_pause(self.r,self.db,self.j));self.assertIsNotNone(self.j.current())
 async def test_old_confirmed_case_isolated_after_upgrade_without_new_event(self):
  from incident_isolation import reconcile_history
  self.r.record('history_missing',message_ids=[100],source='two_successful_checks')
  reconcile_history(self.r,self.db)
  with self.assertRaises(PeerQuarantined):require_peer_allowed(77,self.r)
  require_peer_allowed(88,self.r)
 async def test_center_merges_delete_and_confirmation(self):
  store=AuthorityStore(self.root/'authority.sqlite');store.register(42,'manager_1:42');store.assign(42,'dvk');notices=OwnerNotifications(store)
  for kind,eid in [('telegram_deleted','a'*32),('history_missing','b'*32)]:
   row=dict(kind=kind,run_id='c'*32,recorded_at=int(time.time()),message_ids=[100],source='tracked_private_history' if kind=='telegram_deleted' else 'two_successful_checks',reason_code='peer_isolated')
   data=dict(installation='install',data=dict(table='monitor_events',row=row));m.center_notice(store,notices,42,eid,data);m.center_notice(store,notices,42,eid,data)
  with store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_notices').fetchone()[0],1)
if __name__=='__main__':unittest.main(verbosity=2)
