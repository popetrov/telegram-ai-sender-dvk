import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import access_runtime as runtime
from access_lease import LeaseStore, issue
from sync_protocol import receipt


class ServiceChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Ed25519PrivateKey.generate()
        self.store = LeaseStore(Path(self.tmp.name)/'lease.json', self.key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw), 'm:42', lambda:100)

    def message(self, text, out=False, filename=None, ident=1):
        return SimpleNamespace(id=ident, raw_text=text, out=out, file=SimpleNamespace(name=filename))

    def test_admin_commands_and_normal_messages_are_never_selected(self):
        for text in ['/start', '/users', '/approve 42 dvk', 'Центр работает.', 'hello']:
            for out in [True, False]:
                self.assertFalse(runtime.is_service_message(self.message(text, out), self.store))

    def test_only_own_exact_service_uploads_are_selected(self):
        self.assertTrue(runtime.is_service_message(self.message('DVK_SYNC_V1',True,'encrypted.dvk'), self.store))
        self.assertFalse(runtime.is_service_message(self.message('DVK_SYNC_V1',False,'encrypted.dvk'), self.store))
        self.assertFalse(runtime.is_service_message(self.message('DVK_SYNC_V1',True,'important.xlsx'), self.store))
        request = base64.urlsafe_b64encode(json.dumps({'subject':'m:42'}).encode()).decode()
        self.assertTrue(runtime.is_service_message(self.message('/lease '+request, True), self.store))

    def test_receipts_need_valid_signature_and_matching_installation(self):
        envelope = receipt(self.key, 'm:42', 'a'*32, 'digest')
        text = 'DVK_RECEIPT '+json.dumps(envelope)
        self.assertTrue(runtime.is_service_message(self.message(text), self.store))
        for key, subject in [(self.key,'m:43'),(Ed25519PrivateKey.generate(),'m:42')]:
            text = 'DVK_RECEIPT '+json.dumps(receipt(key,subject,'a'*32,'digest'))
            self.assertFalse(runtime.is_service_message(self.message(text), self.store))

    async def test_cleanup_failure_does_not_invalidate_saved_access(self):
        self.store.accept(issue(self.key, subject='m:42', nonce='a'*48, allowed=True, now=100),'a'*48)
        client = SimpleNamespace(delete_messages=AsyncMock(side_effect=RuntimeError('network')))
        await runtime.cleanup_service_messages(client,'center',self.message('service'))
        self.store.require_access()

    async def test_refresh_preserves_messages_and_saves_verified_lease(self):
        envelope = issue(self.key,subject='m:42',nonce='a'*48,allowed=True,now=100)
        response = self.message('DVK_ACCESS '+json.dumps(envelope), ident=2)
        conversation = SimpleNamespace(send_message=AsyncMock(return_value=self.message('request')),
                                       get_response=AsyncMock(return_value=response))
        from unittest.mock import MagicMock
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=conversation)
        manager.__aexit__ = AsyncMock(return_value=False)
        async def delete(authority, ids, revoke):
            self.store.require_access()
            self.assertEqual(ids,[1,2])
            self.assertTrue(revoke)
        client = SimpleNamespace(conversation=Mock(return_value=manager), delete_messages=AsyncMock(side_effect=delete))
        with patch('access_runtime.secrets.token_hex',return_value='a'*48):
            await runtime.refresh(client,'center',self.store)
        client.delete_messages.assert_not_awaited()
        self.assertTrue(conversation.send_message.call_args.kwargs['silent'])
        self.store.connection_id = 'b'*32
        client.delete_messages.reset_mock()
        with patch('access_runtime.secrets.token_hex',return_value='a'*48):
            await runtime.refresh(client,'center',self.store)
        encoded = conversation.send_message.call_args.args[0].split(' ',1)[1]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(encoded))['connection_id'],'b'*32)

    async def test_history_cleanup_preserves_administration(self):
        messages = [self.message('/start',True,ident=1), self.message('DVK_SYNC_V1',True,'encrypted.dvk',2),
                    self.message('Центр работает.',ident=3)]
        async def history(*args, **kwargs):
            for message in messages:
                yield message
        client = SimpleNamespace(iter_messages=history, delete_messages=AsyncMock())
        await runtime.clean_service_history(client,'center',self.store)
        client.delete_messages.assert_not_awaited()
