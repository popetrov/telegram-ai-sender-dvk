import base64
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault('TELEGRAM_API_ID', '12345')
os.environ.setdefault('TELEGRAM_API_HASH', 'test')
os.environ.setdefault('OPENAI_API_KEY', 'test')
os.environ.setdefault('BOT_TOKEN', '123:test')
os.environ.setdefault('MANAGER_ID', 'test')

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from access_lease import issue, LeaseStore, AccessDenied
from authority_store import AuthorityStore, renewal_end
from file_lock import FileLock
import sync_data
import sync_protocol as protocol
import sync_runtime
import database
import config
import access_server
import schedule_state
from center_backup import backup


def timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Dubai')).timestamp())


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthorityStore(self.root / 'authority.sqlite')
        self.store.register(42, 'manager:42')

    def test_dvk_perpetual_but_manager_revocable(self):
        self.store.assign(42, 'dvk')
        self.assertEqual(self.store.permission(42, 'manager:42', now=10**10), (True, None))
        self.store.revoke(42)
        self.assertFalse(AuthorityStore(self.store.path).permission(42, 'manager:42')[0])

    def test_identity_cannot_be_rebound_by_local_manager_id(self):
        self.store.assign(42, 'dvk')
        self.store.register(42, 'fake:42')
        self.assertFalse(self.store.permission(42, 'fake:42')[0])
        self.assertFalse(self.store.permission(43, 'manager:42')[0])

    def test_calendar_month_end_and_leap_year(self):
        for start, end in [('2024-01-31T12:00:00', '2024-03-02T12:00:00'),
                           ('2025-01-31T12:00:00', '2025-03-02T12:00:00'),
                           ('2026-12-31T12:00:00', '2027-02-02T12:00:00')]:
            with self.subTest(start=start):
                self.assertEqual(renewal_end(None, timestamp(start)), timestamp(end))

    def test_active_renewal_from_end_expired_renewal_from_now(self):
        self.store.company('buyer')
        first = self.store.renew('buyer', timestamp('2026-01-15T10:00:00'))
        second = self.store.renew('buyer', timestamp('2026-01-20T10:00:00'))
        self.assertEqual(second, renewal_end(first, timestamp('2026-01-20T10:00:00')))
        third = self.store.renew('buyer', timestamp('2026-10-01T10:00:00'))
        self.assertEqual(third, timestamp('2026-11-03T10:00:00'))

    def test_company_subscription_covers_multiple_managers(self):
        self.store.company('buyer')
        self.store.register(43, 'other:43')
        self.store.assign(42, 'buyer')
        self.store.assign(43, 'buyer')
        self.assertFalse(self.store.permission(42, 'manager:42', now=100)[0])
        end = self.store.renew('buyer', 100)
        for account, subject in [(42, 'manager:42'), (43, 'other:43')]:
            self.assertTrue(self.store.permission(account, subject, now=end-1)[0])
            self.assertFalse(self.store.permission(account, subject, now=end)[0])
        self.store.company('buyer', False)
        self.assertFalse(self.store.permission(42, 'manager:42', now=end-1)[0])

    def test_lease_never_outlives_payment_after_restart(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        path = self.root / 'lease.json'
        lease = LeaseStore(path, public, 'manager:42', lambda: 100)
        lease.accept(issue(key, subject='manager:42', nonce='n', allowed=True, now=100, subscription_end=200), 'n')
        with self.assertRaises(AccessDenied):
            LeaseStore(path, public, 'manager:42', lambda: 200).require_access()

    def test_receipt_idempotency_and_conflict(self):
        self.store.assign(42, 'dvk')
        data = {'installation': 'a', 'data': {'table': 'clients', 'row': {'id': 1}}}
        digest = protocol.event_digest(data)
        for _ in range(2):
            self.store.receive(42, 'manager:42', 'a'*32, digest, data)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.store.receive(42, 'manager:42', 'a'*32, 'different', data)
        with self.assertRaises(ValueError):
            self.store.receive(43, 'manager:42', 'b'*32, digest, data)

    def test_revocation_does_not_lose_previously_collected_data(self):
        self.store.assign(42, 'dvk')
        self.store.revoke(42)
        self.store.receive(42, 'manager:42', 'a'*32, 'digest', {'previous': True})
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT company_id FROM events').fetchone()[0], 'dvk')

    def test_encrypted_upload_cannot_be_read_or_modified(self):
        key = X25519PrivateKey.generate()
        payload = {'confidential': 'client-company-data'}
        packet = protocol.encrypt(protocol.public_text(key), payload)
        self.assertNotIn(b'client-company-data', packet)
        self.assertEqual(protocol.decrypt(key, packet), payload)
        with self.assertRaises(Exception):
            protocol.decrypt(X25519PrivateKey.generate(), packet)
        altered = json.loads(packet)
        data = bytearray(base64.b64decode(altered['data']))
        data[0] ^= 1
        altered['data'] = protocol.b64(data)
        with self.assertRaises(Exception):
            protocol.decrypt(key, json.dumps(altered).encode())

    def test_signed_receipt_is_bound_to_subject_event_and_content(self):
        key = Ed25519PrivateKey.generate()
        envelope = protocol.receipt(key, 'm:42', 'a'*32, 'digest')
        protocol.verify_receipt(key.public_key(), envelope, 'm:42', 'a'*32, 'digest')
        for subject, event_id, digest in [('m:43', 'a'*32, 'digest'), ('m:42', 'b'*32, 'digest'), ('m:42', 'a'*32, 'wrong')]:
            with self.assertRaises(ValueError):
                protocol.verify_receipt(key.public_key(), envelope, subject, event_id, digest)
        with self.assertRaises(Exception):
            protocol.verify_receipt(Ed25519PrivateKey.generate().public_key(), envelope, 'm:42', 'a'*32, 'digest')

    def test_outbox_is_transactional_and_survives_restart(self):
        path = self.root / 'local.sqlite'
        with patch.object(config, 'DATABASE_PATH', str(path)):
            database.init_database()
            with database.get_connection() as db:
                db.execute("INSERT INTO uploaded_rows(data) VALUES ('client')")
            with self.assertRaises(RuntimeError):
                with database.get_connection() as db:
                    db.execute("INSERT INTO uploaded_rows(data) VALUES ('rolled-back')")
                    raise RuntimeError('rollback')
            database.init_database()
            with database.get_connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0], 1)
                event_id, event = sync_data.next_event(db)
                self.assertEqual(event['data']['row']['data'], 'client')
                self.assertNotIn('BOT_TOKEN', json.dumps(event))

    def test_existing_records_export_once_and_credentials_excluded(self):
        path = self.root / 'local.sqlite'
        with patch.object(config, 'DATABASE_PATH', str(path)):
            database.init_database()
            with database.get_connection() as db:
                db.execute("DELETE FROM local_settings WHERE key='sync_initial_export'")
                db.execute("INSERT INTO uploaded_rows(data) VALUES ('old')")
                db.execute('DELETE FROM sync_outbox')
                db.execute("INSERT INTO local_settings VALUES ('BOT_TOKEN','secret','now')")
            database.init_database()
            database.init_database()
            with database.get_connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0], 1)
                self.assertNotIn('secret', json.dumps(sync_data.next_event(db)))

    def test_process_lock_rejects_second_instance_then_releases(self):
        path = self.root / 'process.lock'
        with FileLock(path):
            with self.assertRaises(RuntimeError):
                with FileLock(path):
                    self.fail('second process acquired lock')
        with FileLock(path):
            pass

    def test_schedule_recovers_only_as_interrupted(self):
        path = self.root / 'schedule.json'
        schedule_state.save({'state':'scheduled', 'send_at':datetime.now(timezone.utc),
                             'items':[{'prepared_id':1}]}, path)
        restored = schedule_state.recover(path)
        self.assertEqual(restored['state'], 'interrupted')
        self.assertEqual(restored['items'], [])
        schedule_state.clear(path)
        self.assertIsNone(schedule_state.recover(path))

    def test_backup_can_restore_database_and_keys(self):
        import zipfile
        state = self.root
        for name in ('signing.key', 'encryption.key', 'public_key.txt', 'sync_public_key.txt'):
            (state / name).write_bytes(b'test-key')
        destination = self.root.parent / (self.root.name + '-backups')
        self.addCleanup(__import__('shutil').rmtree, destination, True)
        target = backup(state, destination)
        self.assertEqual(backup(state, destination), target)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read('signing.key'), b'test-key')
            restore = self.root / 'restore'
            archive.extractall(restore)
        with closing(sqlite3.connect(restore / 'authority.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_handler_lease_is_capped_by_company_end(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityStore(Path(directory) / 'authority.sqlite')
            store.company('buyer')
            store.register(42, 'manager:42')
            store.assign(42, 'buyer')
            with store.connection() as db:
                db.execute("UPDATE companies SET expires_at=200 WHERE id='buyer'")
            key = Ed25519PrivateKey.generate()
            app = access_server.create_server('123:test', 99, key, store)
            callback = next(h.callback for h in app.handlers[0] if getattr(h,'commands',None) and 'lease' in h.commands)
            request = base64.urlsafe_b64encode(json.dumps(dict(subject='manager:42', nonce='a'*48)).encode()).decode()
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                     effective_user=SimpleNamespace(id=42), effective_message=message)
            with patch('authority_store.time.time', return_value=100), patch('access_lease.time.time', return_value=100):
                await callback(update, SimpleNamespace(args=[request]))
            envelope = json.loads(message.reply_text.call_args.args[0].removeprefix('DVK_ACCESS '))
            self.assertTrue(envelope['payload']['allowed'])
            self.assertEqual(envelope['payload']['expires_at'], 200)

    async def test_manager_bot_is_bound_to_telethon_account(self):
        import bot
        import access_runtime
        with patch.object(access_runtime, 'account_id', 42), patch('access_runtime.require_access'):
            update = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(type='private'))
            self.assertTrue(bot.user_is_allowed(update))
            update.effective_user.id = 43
            self.assertFalse(bot.user_is_allowed(update))
            update.effective_user.id = 42
            update.effective_chat.type = 'group'
            self.assertFalse(bot.user_is_allowed(update))

    async def test_receiver_commits_before_ack_and_retries_do_not_duplicate(self):
        from telegram.ext import Application
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityStore(Path(directory) / 'authority.sqlite')
            store.register(42, 'manager:42')
            store.assign(42, 'dvk')
            signer, cipher = Ed25519PrivateKey.generate(), X25519PrivateKey.generate()
            app = Application.builder().token('123:test').build()
            sync_runtime.register_receiver(app, store, signer, cipher)
            callback = app.handlers[0][0].callback
            data = dict(installation='install', data={'table':'clients','row':{'id':1}})
            packet = protocol.encrypt(protocol.public_text(cipher), dict(subject='manager:42', event_id='a'*32, data=data))
            remote = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(packet)))
            document = SimpleNamespace(file_size=len(packet), get_file=AsyncMock(return_value=remote))
            message = SimpleNamespace(caption='DVK_SYNC_V1', document=document, reply_text=AsyncMock())
            update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                     effective_user=SimpleNamespace(id=42), effective_message=message)
            message.reply_text.side_effect = RuntimeError('ack lost')
            await callback(update, None)
            message.reply_text.side_effect = None
            await callback(update, None)
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
            envelope = json.loads(message.reply_text.call_args.args[0].removeprefix('DVK_RECEIPT '))
            protocol.verify_receipt(signer.public_key(), envelope, 'manager:42', 'a'*32, protocol.event_digest(data))

    async def test_company_admin_command_rejects_non_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityStore(Path(directory) / 'authority.sqlite')
            app = access_server.create_server('123:test', 99, Ed25519PrivateKey.generate(), store)
            callback = next(h.callback for h in app.handlers[0] if getattr(h,'commands',None) and 'company' in h.commands)
            message = SimpleNamespace(text='/company attacker', reply_text=AsyncMock())
            update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                     effective_user=SimpleNamespace(id=42), effective_message=message)
            await callback(update, SimpleNamespace(args=['attacker']))
            self.assertEqual([c['id'] for c in store.list_companies()], ['dvk'])
