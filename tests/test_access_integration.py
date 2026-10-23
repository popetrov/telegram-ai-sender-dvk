import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for name, value in dict(TELEGRAM_API_ID="12345", TELEGRAM_API_HASH="test",
                        OPENAI_API_KEY="test", BOT_TOKEN="123:test", MANAGER_ID="test").items():
    os.environ.setdefault(name, value)

from access_lease import AccessDenied, LeaseStore, issue
import access_runtime
import access_server
import bot
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class AccessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_queue_stops_without_sending_or_mutating_items(self):
        item = {"prepared_status": "prepared", "inn": "123", "username": "test"}
        queue = dict(send_at=datetime.now().astimezone(), state="scheduled",
                     chat_id=42, items=[item], owner_user_id=42)
        application = SimpleNamespace(bot_data={bot.QUEUE_KEY: queue},
                                      bot=SimpleNamespace(send_message=AsyncMock()))
        with patch("access_runtime.require_access", side_effect=AccessDenied("revoked")), \
             patch("bot.send_one_prepared_message", new_callable=AsyncMock) as send:
            await bot.send_queue_at_time(application, queue)
        send.assert_not_awaited()
        self.assertEqual(queue["state"], "interrupted")
        self.assertEqual(item["prepared_status"], "prepared")
        self.assertIs(application.bot_data[bot.QUEUE_KEY], queue)

    async def test_no_ai_work_without_access(self):
        with patch("access_runtime.require_access", side_effect=AccessDenied("expired")), \
             patch("bot.generate_company_analysis") as generate:
            status, _, item = await bot.prepare_one_client({})
        self.assertEqual(status, "manual_review")
        self.assertIsNone(item)
        generate.assert_not_called()

    async def test_no_direct_send_without_access(self):
        with patch("access_runtime.require_access", side_effect=AccessDenied("revoked")), \
             patch.object(bot.telegram_client, "send_message", new_callable=AsyncMock) as send:
            with self.assertRaises(AccessDenied):
                await bot.send_one_prepared_message({})
        send.assert_not_awaited()

    async def test_refresh_uses_signed_response_for_current_nonce(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Ed25519PrivateKey.generate()
            store = LeaseStore(Path(directory)/"lease.json", key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw), "manager:42", lambda: 100000)
            conversation = SimpleNamespace(send_message=AsyncMock(), get_response=AsyncMock())
            conversation.get_response.return_value = SimpleNamespace(raw_text="DVK_ACCESS " + json.dumps(
                issue(key, subject=store.subject, nonce="a"*48, allowed=True, now=100000)))
            manager = unittest.mock.MagicMock()
            manager.__aenter__ = AsyncMock(return_value=conversation)
            manager.__aexit__ = AsyncMock(return_value=False)
            client = SimpleNamespace(conversation=unittest.mock.Mock(return_value=manager))
            with patch("access_runtime.secrets.token_hex", return_value="a"*48):
                await access_runtime.refresh(client, "central_bot", store)
            store.require_access()

    async def test_non_admin_cannot_approve(self):
        application = access_server.create_server("123:test", 99, Ed25519PrivateKey.generate())
        callback = next(h.callback for h in application.handlers[0] if "approve" in h.commands)
        message = SimpleNamespace(text="/approve 42", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(type="private"),
                                 effective_message=message)
        with patch("access_server.save_json") as save:
            await callback(update, SimpleNamespace(args=["42"]))
        save.assert_not_called()

    async def test_admin_revoke_persists(self):
        application = access_server.create_server("123:test", 99, Ed25519PrivateKey.generate())
        callback = next(h.callback for h in application.handlers[0] if "revoke" in h.commands)
        message = SimpleNamespace(text="/revoke 42", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=99),
                                 effective_chat=SimpleNamespace(type="private"),
                                 effective_message=message)
        registry = {"42": {"subject": "manager:42", "allowed": True}}
        with patch("access_server.load_registry", return_value=registry), \
             patch("access_server.save_json") as save:
            await callback(update, SimpleNamespace(args=["42"]))
        self.assertFalse(save.call_args.args[1]["42"]["allowed"])

    async def test_authority_signs_denial_for_revoked_account(self):
        key = Ed25519PrivateKey.generate()
        application = access_server.create_server("123:test", 99, key)
        callback = next(h.callback for h in application.handlers[0] if "lease" in h.commands)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(type="private"),
                                 effective_message=message)
        request = {"subject": "manager:42", "nonce": "a"*48}
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
        registry = {"42": {"subject": "manager:42", "allowed": False}}
        with patch("access_server.load_registry", return_value=registry):
            await callback(update, SimpleNamespace(args=[encoded], bot=SimpleNamespace(send_message=AsyncMock())))
        text = message.reply_text.call_args.args[0]
        envelope = json.loads(text[len("DVK_ACCESS "):])
        with tempfile.TemporaryDirectory() as directory:
            store = LeaseStore(Path(directory)/"lease.json", key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw), "manager:42")
            store.accept(envelope, "a"*48)
            with self.assertRaisesRegex(AccessDenied, "revoked"):
                store.require_access()

    async def test_registry_binds_account_and_installation(self):
        registry = {"42": {"subject": "manager:42", "allowed": True}}
        self.assertTrue(access_server.authorized_request(registry, 42, "manager:42"))
        self.assertFalse(access_server.authorized_request(registry, 43, "manager:42"))
        self.assertFalse(access_server.authorized_request(registry, 42, "other:42"))
        registry["42"]["allowed"] = False
        self.assertFalse(access_server.authorized_request(registry, 42, "manager:42"))


if __name__ == "__main__":
    unittest.main()
