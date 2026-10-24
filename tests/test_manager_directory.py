import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from authority_store import AuthorityStore
from manager_directory import ManagerDirectory
from sheets_directory_sync import send_profile,signature
import sheets_directory_sync
from access_server import create_server
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=AuthorityStore(Path(self.temp.name)/'authority.sqlite')
        self.store.register(42,'manager:42');self.store.assign(42,'dvk')
        self.directory=ManagerDirectory(self.store)

    def fill(self,account=42,number='001'):
        self.directory.begin(99,account)
        for value in ('Private Name',number,'Private Direction','Private Company'):
            result=self.directory.answer(99,value)
        return result

    def test_profile_is_durable_private_and_revision_ack_cannot_lose_new_edit(self):
        self.fill();first=self.directory.profile(42)
        self.assertEqual(first['number'],'001')
        self.fill();self.directory.delivered(42,first['revision'])
        self.assertEqual(len(self.directory.pending()),1)
        reboot=ManagerDirectory(self.store)
        self.assertEqual(reboot.profile(42)['revision'],2)
        reboot.delivered(42,2)
        self.assertEqual(reboot.pending(),[])
        allowed,_=self.store.permission(42,'manager:42')
        self.assertTrue(allowed)
        self.assertNotIn('Private Name',json.dumps(self.store.list_managers()))

    def test_draft_survives_restart_and_unique_number_does_not_overwrite_profile(self):
        self.fill()
        self.store.register(43,'manager:43');self.store.assign(43,'dvk')
        self.directory.begin(99,43);self.directory.answer(99,'Second Name')
        self.directory=ManagerDirectory(self.store)
        self.directory.answer(99,'001');self.directory.answer(99,'Other')
        with self.assertRaises(ValueError): self.directory.answer(99,'Company')
        self.assertIsNone(self.directory.profile(43))
        self.assertEqual(self.directory.profile(42)['full_name'],'Private Name')

    async def test_admin_only_wizard_and_service_lease_never_contains_directory(self):
        app=create_server('123:test',99,Ed25519PrivateKey.generate(),self.store,
                          role='admin',directory=self.directory)
        handler=next(h for h in app.handlers[0] if 'profile' in getattr(h,'commands',set()))
        reply=AsyncMock()
        update=SimpleNamespace(effective_user=SimpleNamespace(id=42),
                               effective_chat=SimpleNamespace(type='private'),
                               effective_message=SimpleNamespace(reply_text=reply))
        await handler.callback(update,SimpleNamespace(args=['42']))
        reply.assert_not_awaited()
        update.effective_user.id=99
        await handler.callback(update,SimpleNamespace(args=['42']))
        reply.assert_awaited_once()
        self.fill()
        service=create_server('123:test',99,Ed25519PrivateKey.generate(),self.store,role='service')
        self.assertFalse(any('profile' in getattr(h,'commands',set()) for h in service.handlers[0]))
        import base64
        lease=next(h for h in service.handlers[0] if 'lease' in getattr(h,'commands',set()))
        payload=dict(subject='manager:42',nonce='a'*48)
        update.effective_user.id=42
        await lease.callback(update,SimpleNamespace(args=[base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()]))
        response=reply.call_args.args[0]
        self.assertNotIn('Private',response)
        self.assertNotIn('full_name',response)

    def test_google_receipt_is_signed_request_bound_and_contains_only_roster_fields(self):
        self.fill();profile=self.directory.profile(42)
        settings=dict(format=1,url='https://script.google.com/macros/s/test/exec',secret='a'*64)
        def opener(request,timeout):
            outer=json.loads(request.data);self.assertEqual(outer['signature'],signature(settings['secret'],outer['message']))
            data=json.loads(outer['message'])
            self.assertNotIn('delivered_revision',data['profile'])
            signed=json.dumps(dict(request_id=data['request_id'],account_id=42,revision=profile['revision'],ok=True))
            return io.BytesIO(json.dumps(dict(message=signed,signature=signature(settings['secret'],signed))).encode())
        send_profile(settings,profile,opener=opener)
        with self.assertRaises(ValueError):
            send_profile(settings,profile,opener=lambda *a,**kw:io.BytesIO(b'{"message":"{}","signature":"fake"}'))
        self.assertEqual(len(self.directory.pending()),1) # client alone never marks delivery.

    async def test_network_failure_keeps_profile_pending_and_notifies_only_owner_channel(self):
        self.fill()
        settings=dict(format=1,url='https://script.google.com/macros/s/test/exec',secret='a'*64)
        state=Path(self.temp.name)
        (state/'sheets.json').write_text(json.dumps(settings))
        notices=SimpleNamespace(error=unittest.mock.Mock())
        with patch('sheets_directory_sync.send_profile',side_effect=TimeoutError), \
             patch('sheets_directory_sync.asyncio.sleep',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await sheets_directory_sync.loop(self.directory,state,notices)
        self.assertEqual(len(self.directory.pending()),1)
        notices.error.assert_called_once()
        self.assertEqual(notices.error.call_args.args[0],'Google-таблица')

    @unittest.skipUnless(shutil.which('node'),'Node runtime unavailable for Apps Script stand-in')
    def test_actual_apps_script_handler_with_google_api_stand_in(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/'tests/roster_bridge_harness.js').read_text('utf-8').replace(
            "fs.readFileSync(process.argv[2],'utf8')",json.dumps((root/'google_roster_bridge.gs').read_text('utf-8')))
        result=subprocess.run(['node','-e',script],
                              capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
