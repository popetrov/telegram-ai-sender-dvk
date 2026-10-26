import asyncio,base64,json,tempfile,time,unittest,shutil,subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app_profile_protocol import issue,open_response,canonical,DOMAIN
from app_profile_client import new_request,accept,cached_binding,check_account,prepare
from sync_protocol import public_text
from authority_store import AuthorityStore
from app_profile_server import AppProfileService
import telegram_app_migrate as migration


class Profiles(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node runtime unavailable')
    def test_actual_broker_script(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/'tests/app_broker_test.cjs').read_text('utf-8').replace(
            "fs.readFileSync('google_roster_bridge.gs','utf8')",json.dumps((root/'google_roster_bridge.gs').read_text('utf-8')))
        result=subprocess.run(['node','-e',script],cwd=root,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.center=Ed25519PrivateKey.generate();self.owner=Ed25519PrivateKey.generate()
        self.public=dict(format=1,service_bot='dvk_test_bot',access_public_key=public_text(self.center),sync_public_key=public_text(self.center))
        (self.root/'center-public.json').write_text(json.dumps(self.public))
        (self.root/'.env').write_text('MANAGER_ID=manager_007\nSESSION_NAME=manager_007\nBOT_TOKEN=keep\n')
        payload=dict(protocol='DVK_TELEGRAM_APP_PROFILE_V1',version=1,api_id=12345,api_hash='a'*32,app_title='Synthetic test')
        self.profile=dict(payload=payload,signature=base64.b64encode(self.owner.sign(migration.DOMAIN+canonical(payload))).decode())
        self.key,self.request=new_request('manager_007')

    def packet(self,now=None):return issue(self.center,self.request,777,self.profile,now=now)

    def test_only_recipient_can_read_and_response_is_bound_to_request_account_time(self):
        packet=self.packet(now=1000)
        self.assertNotIn('a'*32,json.dumps(packet));self.assertNotIn('api_hash',json.dumps(packet))
        self.assertEqual(open_response(self.key,packet,self.request,public_text(self.center),now=1001)['payload']['account_id'],777)
        wrong_key,wrong_request=new_request('manager_007')
        for key,request,account,now in [(wrong_key,self.request,None,1001),(self.key,wrong_request,None,1001),
                                       (self.key,self.request,888,1001),(self.key,self.request,None,1900)]:
            with self.assertRaises(Exception):open_response(key,packet,request,public_text(self.center),expected_account=account,now=now)
        packet['data']=packet['data'][:-5]+'AAAA='
        with self.assertRaises(Exception):open_response(self.key,packet,self.request,public_text(self.center),now=1001)

    def test_owner_signature_checked_before_any_local_settings_change(self):
        original=(self.root/'.env').read_bytes()
        with patch('app_profile_client.PROFILE_SIGNING_PUBLIC_KEY',public_text(Ed25519PrivateKey.generate())):
            with self.assertRaises(Exception):accept(self.root,self.key,self.packet(),self.request,self.public)
        self.assertEqual(original,(self.root/'.env').read_bytes())
        self.assertFalse((self.root/'data/telegram-app-binding.private.json').exists())

    def test_durable_binding_rejects_wrong_login_or_tampering(self):
        with patch('app_profile_client.PROFILE_SIGNING_PUBLIC_KEY',public_text(self.owner)):
            accept(self.root,self.key,self.packet(),self.request,self.public)
        check_account(self.root,777)
        with self.assertRaises(RuntimeError):check_account(self.root,888)
        path=self.root/'data/telegram-app-binding.private.json';data=json.loads(path.read_text())
        data['payload']['account_id']=888;path.write_text(json.dumps(data))
        with self.assertRaises(Exception):check_account(self.root,888)

    def test_unavailable_center_leaves_existing_env_database_session_queue_identical(self):
        env=self.root/'.env';env.write_text(env.read_text()+'TELEGRAM_API_ID=123\nTELEGRAM_API_HASH='+'b'*32+'\n')
        for name in ['manager_007.session','clients.sqlite','queue.json']:(self.root/name).write_bytes(b'unchanged')
        original={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch('app_profile_client.direct',new=AsyncMock(side_effect=TimeoutError)):
            self.assertEqual(prepare(self.root)['status'],'deferred')
        self.assertEqual(original,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_fresh_setup_is_blocked_without_approved_delivery(self):
        with patch('app_profile_client.fresh',side_effect=TimeoutError):
            with self.assertRaises(RuntimeError):prepare(self.root)
        self.assertNotIn('TELEGRAM_API_HASH',(self.root/'.env').read_text())

    def test_direct_delivery_requires_company_access_and_preserves_manager_number(self):
        state=self.root/'state';state.mkdir();(state/'telegram-app-profile.private.json').write_text(json.dumps(self.profile))
        store=AuthorityStore(state/'authority.sqlite')
        with patch('app_profile_server.PROFILE_SIGNING_PUBLIC_KEY',public_text(self.owner)):
            service=AppProfileService(store,self.center,state)
        message=SimpleNamespace(reply_text=AsyncMock())
        update=SimpleNamespace(effective_chat=SimpleNamespace(type='private'),effective_user=SimpleNamespace(id=777,is_bot=False),effective_message=message)
        context=SimpleNamespace(args=[base64.urlsafe_b64encode(canonical(self.request)).decode()])
        asyncio.run(service.direct(update,context));self.assertEqual(message.reply_text.call_args.args[0],'DVK_APP_PENDING')
        store.assign(777,'dvk')
        with patch('app_profile_server.PROFILE_SIGNING_PUBLIC_KEY',public_text(self.owner)):
            asyncio.run(service.direct(update,context))
        response=message.reply_text.call_args.args[0];self.assertTrue(response.startswith('DVK_APP '));self.assertNotIn('api_hash',response)
        self.assertEqual(open_response(self.key,json.loads(response[8:]),self.request,public_text(self.center))['payload']['manager_id'],'manager_007')
        store.revoke(777);asyncio.run(service.direct(update,context))
        self.assertEqual(message.reply_text.call_args.args[0],'DVK_APP_PENDING')

