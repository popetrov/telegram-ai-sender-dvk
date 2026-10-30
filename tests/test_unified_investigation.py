import asyncio,contextlib,hashlib,json,os,shutil,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import functions,types
import unified_install as u
import incident_monitor as m
from forensic_trace import validate,origin,validate_chain
from primary_identity import bind
from safe_telegram import ProtectedTelegramClient,TelegramDeletionBlocked
from telegram_safety import SafetyJournal

C=Path(__file__).resolve().parents[1]
def payload(version='2.0.40'):
    from manual_update import package_files
    result={n:(C/n).read_bytes() for n in package_files(C)};result['version.txt']=(version+'\n').encode()
    return result

def fixture(root,manager='manager_002',key=b'x'*256):
    root.mkdir();(root/'data').mkdir();(root/'prompts').mkdir()
    (root/'.env').write_text('MANAGER_ID='+manager+'\nSESSION_NAME=telegram_sender\n',encoding='utf-8')
    (root/'products.json').write_bytes((C/'products.json').read_bytes())
    with contextlib.closing(sqlite3.connect(root/'telegram_sender.session')) as db,db:
        db.execute('CREATE TABLE sessions(auth_key BLOB)');db.execute('INSERT INTO sessions VALUES (?)',(key,))
    with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db,db:
        db.execute('CREATE TABLE messages(id INTEGER,status TEXT)');db.execute("INSERT INTO messages VALUES(1,'uncertain')")
    return root

class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.base=Path(self.t.name)
    def test_move_then_remove_source_has_one_data_root(self):
        root=fixture(self.base/'launcher');source=fixture(self.base/'launcher_dvk_old')
        with contextlib.closing(sqlite3.connect(source/'data/local.sqlite')) as db,db:db.execute("UPDATE messages SET status='sent'")
        code=u.prepare_code(root,payload())
        checked=[];u.activate(root,code,source,check=lambda r,c:checked.append((r,c)))
        u.retire(source,root)
        self.assertTrue(checked);self.assertIn('runpy', (source/'main.py').read_text('utf-8'))
        shutil.rmtree(source)
        self.assertEqual(u.runtime(root),code)
        self.assertFalse((code/'.env').exists());self.assertFalse((code/'data').exists())
        from installation_paths import state_root
        self.assertEqual(state_root(code/'main.py'),root.resolve())
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'sent')
    def test_skip_one_hundred_releases_and_bounded_code(self):
        root=fixture(self.base/'launcher')
        for version in ['2.0.38','2.0.40','2.0.140','2.0.240']:
            code=u.prepare_code(root,payload(version));u.activate(root,code,root,check=lambda *a:None);u.prune(root)
            self.assertLessEqual(len(list((root/'.dvk/runtime').iterdir())),2)
            self.assertEqual((u.runtime(root)/'version.txt').read_text().strip(),version)
        self.assertEqual({p.name for p in self.base.iterdir()},{'launcher'})
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'uncertain')
    def test_accidental_sibling_launch_recovers_verified_original_anchor(self):
        root=fixture(self.base/'launcher')
        source=fixture(self.base/('launcher_dvk_'+uuid.uuid4().hex))
        (root/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=source.name)))
        self.assertEqual(u.canonical_root(source,source),root.resolve())
        (root/'data/active-installation.json').unlink()
        self.assertEqual(u.canonical_root(source,source),source.resolve())
    def test_other_old_copy_database_is_archived_without_overwriting_active(self):
        root=fixture(self.base/'launcher');other=fixture(self.base/'old')
        with contextlib.closing(sqlite3.connect(other/'data/local.sqlite')) as db,db:
            db.execute("UPDATE messages SET status='old-copy-only'")
        u.activate(root,u.prepare_code(root,payload()),root,check=lambda *a:None)
        u.retire(other,root)
        archived=list((root/'.dvk/recovered-files').rglob('local.sqlite'))
        self.assertEqual(len(archived),1)
        with contextlib.closing(sqlite3.connect(archived[0])) as db:
            self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'old-copy-only')
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:
            self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'uncertain')
    def test_dormant_launcher_schedule_is_archived_and_not_reactivated(self):
        root=fixture(self.base/'launcher');source=fixture(self.base/'current')
        (root/'data/schedule.json').write_text('{"obsolete":true}')
        code=u.prepare_code(root,payload());u.activate(root,code,source,check=lambda *a:None)
        self.assertFalse((root/'data/schedule.json').exists())
        self.assertEqual((root/'.dvk/recovered-launcher-state'/code.name/'data/schedule.json').read_text(),'{"obsolete":true}')
    def test_wrong_session_or_manager_never_merges(self):
        root=fixture(self.base/'launcher');source=fixture(self.base/'wrong',key=b'y'*256);old=(root/'telegram_sender.session').read_bytes()
        with self.assertRaises(ValueError):u.activate(root,u.prepare_code(root,payload()),source,check=lambda *a:None)
        self.assertEqual((root/'telegram_sender.session').read_bytes(),old)
    def test_failed_check_restores_state_and_stops_until_recovery(self):
        root=fixture(self.base/'launcher');source=fixture(self.base/'old')
        with contextlib.closing(sqlite3.connect(source/'data/local.sqlite')) as db,db:db.execute("UPDATE messages SET status='sent'")
        def fail(*a):raise RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):u.activate(root,u.prepare_code(root,payload()),source,check=fail)
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'uncertain')
        with self.assertRaises(ValueError):u.run(root,updates=False,launch_fn=Mock())
    def test_power_loss_recovery_uses_clean_process_and_restores_state(self):
        import subprocess,sys
        root=fixture(self.base/'launcher')
        (root/'start.py').write_text('raise SystemExit(23)\n')
        code=u.prepare_code(root,payload())
        script="""from pathlib import Path
import sys,os,sqlite3
sys.path.insert(0,sys.argv[1]);import unified_install as u
def powercut(root,code):
 db=sqlite3.connect(root/'data/local.sqlite');db.execute("UPDATE messages SET status='sent'");db.commit();db.close();os._exit(71)
u.activate(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[2]),check=powercut)
"""
        result=subprocess.run([sys.executable,'-c',script,str(C),str(root),str(code)])
        self.assertEqual(result.returncode,71)
        self.assertTrue((root/'.dvk/migration-pending.json').exists())
        result=subprocess.run([sys.executable,str(root/'start.py')],cwd=self.base,capture_output=True)
        self.assertEqual(result.returncode,23,result.stderr.decode(errors='replace'))
        self.assertFalse((root/'.dvk/migration-pending.json').exists())
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:
            self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'uncertain')
    def test_code_tamper_cannot_launch(self):
        root=fixture(self.base/'launcher');code=u.prepare_code(root,payload());u.activate(root,code,root,check=lambda *a:None)
        (code/'main.py').write_text('raise Exception("tampered")',encoding='utf-8')
        with self.assertRaises(ValueError):u.runtime(root)
    def test_personal_files_cannot_be_packaged_as_code(self):
        root=fixture(self.base/'launcher')
        with self.assertRaises(ValueError):u.prepare_code(root,{'.env':b'secret'})
    def test_first_legacy_parent_gets_only_removable_data_free_stub(self):
        root=fixture(self.base/'launcher');stub=root.with_name(root.name+'_dvk_'+uuid.uuid4().hex)
        with patch.dict(os.environ,{'DVK_LAUNCHER_ROOT':str(root)}):
            code=u.prepare_legacy(root,stub,payload(),check=lambda *a:None)
        self.assertEqual({p.name for p in stub.iterdir()},{'main.py','DVK_TRANSITION_STUB.json'})
        shutil.rmtree(stub)
        self.assertEqual(u.runtime(root),code)
        self.assertFalse((code/'data').exists())
    def test_failed_schema_change_restores_prior_primary_before_launch(self):
        root=fixture(self.base/'launcher')
        def fail(root,code):
            with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db,db:
                db.execute("UPDATE messages SET status='sent'")
                db.execute('ALTER TABLE messages ADD COLUMN temporary_schema INTEGER')
            raise RuntimeError('after migration')
        with self.assertRaises(RuntimeError):u.activate(root,u.prepare_code(root,payload()),root,check=fail)
        with contextlib.closing(sqlite3.connect(root/'data/local.sqlite')) as db:
            self.assertEqual(db.execute('SELECT status FROM messages').fetchone()[0],'uncertain')
            self.assertNotIn('temporary_schema',{r[1] for r in db.execute('PRAGMA table_info(messages)')})
    def test_migration_chain_runs_missing_steps_once_and_rolls_back_failure(self):
        from data_schema import migrate
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE history(step INTEGER)')
            migrations={i:(lambda d,i=i:d.execute('INSERT INTO history VALUES (?)',(i,))) for i in range(1,101)}
            migrate(db,3,migrations);migrate(db,100,migrations);migrate(db,100,migrations)
            self.assertEqual([r[0] for r in db.execute('SELECT step FROM history')],list(range(1,101)))
            with self.assertRaises(ValueError):migrate(db,2,migrations)
    def test_unrelated_user_files_preserved_before_old_folder_is_removable(self):
        root=fixture(self.base/'launcher');source=fixture(self.base/'old')
        (source/'client-list.xlsx').write_bytes(b'synthetic user data')
        u.activate(root,u.prepare_code(root,payload()),source,check=lambda *a:None);u.retire(source,root)
        info=json.loads((source/'DVK_FOLDER_MOVED.json').read_text('utf-8'));self.assertTrue(info['safe_to_delete'])
        shutil.rmtree(source)
        copies=list((root/'.dvk/recovered-files').rglob('client-list.xlsx'))
        self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_bytes(),b'synthetic user data')

class TraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.r=m.Recorder(self.root/'monitor.sqlite');self.c=ProtectedTelegramClient(MemorySession(),123,'a'*32,safety_journal=SafetyJournal(self.root/'safety.sqlite'))
    def events(self):
        with contextlib.closing(self.r.connect()) as db:return [json.loads(x[0]) for x in db.execute('SELECT data FROM monitor_events ORDER BY sequence')]
    async def test_one_operation_links_intent_queue_and_server_result(self):
        req=functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'SECRET_CUSTOMER_TEXT')
        async def fake_call(_client,sender,request,*a,**kw):
            self.c._trace(request,'transport_queue');return types.UpdateShortSentMessage(101,1,1,None,out=True)
        with patch.object(m,'_default',self.r),patch.object(TelegramClient,'_call',new=fake_call):
            with origin('campaign','messages',15):await self.c._call(SimpleNamespace(),req)
        rows=[r for r in self.events() if r['kind']=='forensic_operation']
        self.assertEqual({r['stage'] for r in rows},{'sdk_intent','transport_queue','server_result'})
        self.assertEqual(len({r['evidence']['op_id'] for r in rows}),1)
        self.assertTrue(all(r['evidence']['origin_id']==15 for r in rows));self.assertNotIn('SECRET_CUSTOMER_TEXT',json.dumps(self.events()))
    async def test_all_delete_targets_chunked_and_blocked_without_network(self):
        req=functions.messages.DeleteMessagesRequest(list(range(1,504)),revoke=True)
        with patch.object(m,'_default',self.r),patch.object(TelegramClient,'_call',new_callable=AsyncMock) as network:
            with self.assertRaises(TelegramDeletionBlocked):await self.c(req)
        network.assert_not_awaited();rows=[r for r in self.events() if r['kind']=='forensic_operation']
        self.assertEqual([i for r in rows for i in r['evidence']['target_ids']],list(range(1,504)))
        self.assertEqual({r['stage'] for r in rows},{'blocked_before_network'})
    async def test_hash_chain_detects_mutation_and_retains_previous_link(self):
        self.r.record('heartbeat',connected=True);self.r.record('heartbeat',connected=True);a,b=self.events()
        self.assertEqual(b['previous_hash'],a['event_hash']);validate_chain(a);validate_chain(b)
        b['connected']=False
        with self.assertRaises(ValueError):m.validate_event(b)
    async def test_wire_guard_rejects_bypass_and_records_socket_for_allowed_request(self):
        req=functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'synthetic')
        state=SimpleNamespace(request=req,msg_id=444,future=asyncio.get_running_loop().create_future())
        sender=SimpleNamespace(_send_queue=SimpleNamespace(get=AsyncMock(return_value=([state],b'plain'))),
                _state=SimpleNamespace(encrypt_message_data=lambda data:b'encrypted'),_connection=SimpleNamespace(_send=Mock()))
        from forensic_transport import install
        install(self.c,sender)
        with patch.object(m,'_default',self.r):
            await sender._send_queue.get();data=sender._state.encrypt_message_data(b'plain');sender._connection._send(data)
            state.request=functions.messages.DeleteMessagesRequest([101])
            self.assertEqual(await sender._send_queue.get(),([],b''))
        self.assertTrue(state.future.done());state.future.exception()
        self.assertTrue(any(r.get('stage')=='socket_write_attempt' for r in self.events()))
    async def test_service_grant_survives_serialization_but_never_changed_request(self):
        from safe_telegram import _report_permission
        req=functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'technical report')
        self.c._safety_failed=True
        token=_report_permission.set((77,'technical report'))
        try:self.c._check_request(req)
        finally:_report_permission.reset(token)
        state=SimpleNamespace(request=req,data=bytes(req),msg_id=12,future=asyncio.get_running_loop().create_future())
        sender=SimpleNamespace(_send_queue=SimpleNamespace(get=AsyncMock(return_value=([state],b'wire'))),
            _state=SimpleNamespace(encrypt_message_data=lambda x:x),_connection=SimpleNamespace(_send=Mock()))
        from forensic_transport import install
        install(self.c,sender)
        with patch.object(m,'_default',self.r):await sender._send_queue.get()
        req.peer=types.InputPeerUser(88,0)
        from telegram_safety import SafetyStop
        with self.assertRaises(SafetyStop):self.c._check_request(req)
        delete=functions.messages.DeleteMessagesRequest([91])
        delete._dvk_service_grant=req._dvk_service_grant
        with patch.object(m,'_default',self.r):
            with self.assertRaises(TelegramDeletionBlocked):self.c._check_request(delete)
    async def test_blocked_batch_resolves_every_waiter_without_socket_write(self):
        loop=asyncio.get_running_loop()
        members=[SimpleNamespace(request=req,data=bytes(req),msg_id=i,future=loop.create_future())
            for i,req in enumerate([functions.messages.DeleteMessagesRequest([91]),
                functions.messages.GetHistoryRequest(types.InputPeerUser(77,0),0,None,0,1,0,0,0)])]
        sender=SimpleNamespace(_send_queue=SimpleNamespace(get=AsyncMock(return_value=(members,b'wire'))),
            _state=SimpleNamespace(encrypt_message_data=lambda x:x),_connection=SimpleNamespace(_send=Mock()))
        from forensic_transport import install
        install(self.c,sender)
        with patch.object(m,'_default',self.r):
            self.assertEqual(await sender._send_queue.get(),([],b''))
        for state in members:
            self.assertTrue(state.future.done());self.assertIsInstance(state.future.exception(),TelegramDeletionBlocked)
        sender._connection._send.assert_not_called()
    async def test_unvalidated_metadata_cannot_export_secrets(self):
        with self.assertRaises(ValueError):validate({'text':'SECRET'})
        with self.assertRaises(ValueError):validate({'trigger':'campaign','origin_table':'SECRET TOKEN'})
    async def test_serialized_delete_cannot_hide_behind_safe_request_object(self):
        declared=functions.messages.GetHistoryRequest(types.InputPeerUser(77,0),0,None,0,1,0,0,0)
        state=SimpleNamespace(request=declared,data=bytes(functions.messages.DeleteMessagesRequest([901],revoke=True)),msg_id=55,future=asyncio.get_running_loop().create_future())
        connection=SimpleNamespace(_send=Mock())
        sender=SimpleNamespace(_send_queue=SimpleNamespace(get=AsyncMock(return_value=([state],b'wire'))),_state=SimpleNamespace(encrypt_message_data=lambda d:d),_connection=connection)
        from forensic_transport import install
        install(self.c,sender)
        with patch.object(m,'_default',self.r):
            self.assertEqual(await sender._send_queue.get(),([],b''))
        self.assertTrue(state.future.done());state.future.exception();connection._send.assert_not_called()
        self.assertTrue(any(901 in r.get('evidence',{}).get('target_ids',[]) for r in self.events()))
    async def test_background_wire_stage_keeps_business_origin_without_context(self):
        req=functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'private')
        before=bytes(req)
        with patch.object(m,'_default',self.r):
            with origin('campaign','messages',19):self.c._trace(req,'sdk_intent')
            self.c._trace(req,'socket_write_attempt',wire_message_id=18)
        self.assertEqual(before,bytes(req))
        self.assertEqual({r['evidence']['origin_id'] for r in self.events()},{19})
    async def test_retention_keeps_unacknowledged_and_incident_window(self):
        with patch('time.time',return_value=100):
            kept=self.r.record('heartbeat',connected=True)
        with patch('time.time',return_value=2000):other=self.r.record('heartbeat',connected=True)
        self.r.preserve_case([901],'telegram_deleted',100)
        self.r.acknowledge(kept);self.r.acknowledge(other)
        self.r.prune_acknowledged(now=10000000)
        self.assertEqual(len(self.events()),1);self.assertEqual(self.events()[0]['recorded_at'],100)
    async def test_last_send_identity_guard_accepts_relative_primary_path(self):
        import primary_identity as identity
        p=self.root/'primary.sqlite'
        with contextlib.closing(sqlite3.connect(p)) as db,db:
            db.execute('CREATE TABLE local_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
            for key,value in [('account_id','42'),('manager_id','manager_test'),('data_store_id','a'*32)]:
                db.execute('INSERT INTO local_settings VALUES(?,?,?)',(key,value,'now'))
        old=Path.cwd()
        try:
            os.chdir(self.root);identity.activate_binding(42,'manager_test','a'*32)
            identity.require_current('primary.sqlite')
            with contextlib.closing(sqlite3.connect(p)) as db,db:db.execute("UPDATE local_settings SET value='43' WHERE key='account_id'")
            with self.assertRaises(ValueError):identity.require_current('primary.sqlite')
        finally:os.chdir(old);identity._bound=None
    async def test_populated_primary_cannot_bind_to_fresh_other_account_monitor(self):
        p=self.root/'primary.sqlite'
        with contextlib.closing(sqlite3.connect(p)) as db,db:
            db.execute('CREATE TABLE local_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
            db.execute("INSERT INTO local_settings VALUES('manager_id','manager_002','now')")
            db.execute('CREATE TABLE clients(id INTEGER)');db.execute('INSERT INTO clients VALUES(1)')
        with self.assertRaises(ValueError):bind(p,self.r,42,'manager_002')
        self.r.bind(42,'manager_002');data_id=bind(p,self.r,42,'manager_002')
        self.assertEqual(len(data_id),32)
        with self.assertRaises(ValueError):bind(p,self.r,43,'manager_002')

if __name__=='__main__':unittest.main()
