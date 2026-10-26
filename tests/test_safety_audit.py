import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from safe_telegram import ProtectedTelegramClient, TelegramDeletionBlocked, require_non_destructive
from telethon.tl.functions.messages import DeleteHistoryRequest, GetHistoryRequest
from transition_setup import same_legacy_runtime
import account_guard


class SafetyAuditTests(unittest.TestCase):
    def test_only_reviewed_legacy_setting_is_accepted(self):
        old = b'head\nMAX_PARALLEL_RESEARCH = 10\ntail\n'
        safe = old.replace(b' = 10', b' = 1')
        self.assertTrue(same_legacy_runtime('bot.py', safe, old))
        for other in (safe + b'changed\n', old.replace(b' = 10', b' = 2')):
            self.assertFalse(same_legacy_runtime('bot.py', other, old))
        self.assertFalse(same_legacy_runtime('main.py', safe, old))

    def test_raw_and_batch_deletions_are_blocked(self):
        request = DeleteHistoryRequest(peer='me', max_id=0)
        for item in (request, [request], (request,)):
            with self.assertRaises(TelegramDeletionBlocked):
                require_non_destructive(item)
        class Wrapper:
            query = request
        with self.assertRaises(TelegramDeletionBlocked):
            require_non_destructive(Wrapper())

    def test_public_deletions_are_blocked_without_client_initialization(self):
        from telegram_safety import SafetyJournal
        with tempfile.TemporaryDirectory() as folder:
            client = object.__new__(ProtectedTelegramClient)
            client.safety_journal = SafetyJournal(Path(folder)/'safety.sqlite')
            for name in ('delete_messages', 'delete_dialog'):
                with self.assertRaises(TelegramDeletionBlocked):
                    asyncio.run(getattr(client, name)('me', [1]))

    def test_account_lock_applies_across_processes_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            script = 'import account_guard,sys;account_guard.claim_account(42,sys.argv[1])'
            account_guard.claim_account(42, directory)
            try:
                result = subprocess.run([sys.executable, '-c', script, directory], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            finally:
                account_guard.release_account()
            result = subprocess.run([sys.executable, '-c', script, directory], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
