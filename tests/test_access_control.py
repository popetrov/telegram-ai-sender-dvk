import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# bot.py imports the Telegram/OpenAI clients and reads required environment
# variables through config.py. These values are test-only placeholders.
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "test-api-hash")
os.environ.setdefault("TELEGRAM_PHONE", "+10000000000")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("MANAGER_ID", "test-manager")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "925600480")

import config
import bot


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.allowed_file = self.root / "data" / "allowed_users.json"
        self.requests_file = self.root / "data" / "access_requests.json"
        self.data_dir = self.root / "data"
        self.patches = [
            patch.object(bot, "ALLOWED_USERS_FILE", self.allowed_file),
            patch.object(bot, "ACCESS_REQUESTS_FILE", self.requests_file),
            patch.object(bot, "DATA_DIR", self.data_dir),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for item in reversed(self.patches):
            item.stop()

    def test_missing_allowlist_starts_closed(self):
        self.assertEqual(bot.load_allowed_users(), set())
        self.assertTrue(self.allowed_file.exists())

    def test_allowed_users_round_trip(self):
        bot.save_allowed_users({30, 10})
        self.assertEqual(bot.load_allowed_users(), {10, 30})

    def test_invalid_allowlist_fails_closed(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_file.write_text('{"allowed_users":"925600480"}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            bot.load_allowed_users()

    def test_invalid_user_id_fails_closed(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_file.write_text('{"allowed_users":[-1]}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            bot.load_allowed_users()

    def test_request_store_round_trip(self):
        expected = {"abc123": {"user_id": 42, "status": "pending"}}
        bot.save_access_requests(expected)
        self.assertEqual(bot.load_access_requests(), expected)

    def test_bad_admin_id_is_not_accepted(self):
        with patch.object(config, "ADMIN_TELEGRAM_ID", "not-an-id"):
            self.assertIsNone(bot.get_admin_telegram_id())

    def test_admin_id_is_parsed(self):
        with patch.object(config, "ADMIN_TELEGRAM_ID", "925600480"):
            self.assertEqual(bot.get_admin_telegram_id(), 925600480)

    def test_unapproved_user_is_denied(self):
        class User:
            id = 42
        class Update:
            effective_user = User()
        self.assertFalse(bot.user_is_allowed(Update()))

    def test_onboarding_explains_request_process(self):
        text = bot.onboarding_text(42)
        self.assertIn("/request_access", text)
        self.assertIn("Telegram ID: 42", text)
        self.assertIn("До одобрения", text)


if __name__ == "__main__":
    unittest.main()
