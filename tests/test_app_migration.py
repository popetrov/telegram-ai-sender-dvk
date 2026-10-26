import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch
from types import SimpleNamespace as Object
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import telegram_app_migrate as migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.env=self.root/'.env'
        self.old=(123456,'a'*32);self.new=(234567,'b'*32)
        self.original=('MANAGER_ID=manager_007\r\nSESSION_NAME=sender_007\r\n'
            'TELEGRAM_PHONE=+10000000000\r\nBOT_TOKEN=keep-this-test-token\r\n'
            'OPENAI_API_KEY=keep-this-test-key\r\n# preserve comments\r\n'
            f'TELEGRAM_API_ID={self.old[0]}\r\nTELEGRAM_API_HASH={self.old[1]}\r\n').encode()
        self.env.write_bytes(self.original)
        self.session=self.root/'sender_007.session';self.session.write_bytes(b'test-session-unchanged')
        (self.root/'data').mkdir();self.db=self.root/'data/local.sqlite';self.db.write_bytes(b'test-business-db-unchanged')
        self.queue=self.root/'data/schedule.json';self.queue.write_text('{"prepared_ids":[1,2]}')
        self.key=Ed25519PrivateKey.generate()
        self.public=base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
        self.profile=self.root/'profile.private.json'
        self.payload=dict(protocol='DVK_TELEGRAM_APP_PROFILE_V1',version=2,api_id=self.new[0],api_hash=self.new[1],app_title='Test App')
        self.sign()
        self.verify=AsyncMock()

    def sign(self):
        packet=dict(payload=self.payload,signature=base64.b64encode(self.key.sign(migration.DOMAIN+migration.canonical(self.payload))).decode())
        self.profile.write_text(json.dumps(packet))

    def migrate(self):return migration.migrate(self.root,self.profile,self.public,verify=self.verify)

    def test_only_api_pair_changes_after_successful_same_session_probe(self):
        before=[p.read_bytes() for p in (self.session,self.db,self.queue)]
        result=self.migrate();self.assertEqual(result['status'],'migrated')
        self.verify.assert_awaited_once()
        args=self.verify.call_args.args;self.assertEqual(args[2:],(self.old,self.new))
        updated=self.env.read_bytes()
        self.assertEqual(updated,self.original.replace(str(self.old[0]).encode(),str(self.new[0]).encode()).replace(self.old[1].encode(),self.new[1].encode()))
        self.assertEqual([p.read_bytes() for p in (self.session,self.db,self.queue)],before)
        backups=list((self.root/'data/telegram-app-backups').glob('*'));self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),self.original)

    def test_failed_network_or_auth_probe_leaves_old_settings_byte_identical(self):
        self.verify.side_effect=migration.MigrationDeferred('network failed')
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.assertEqual(self.env.read_bytes(),self.original)
        self.assertFalse((self.root/'data/telegram-app-migration.json').exists())

    def test_signature_tampering_is_rejected_before_network_or_writes(self):
        envelope=json.loads(self.profile.read_text());envelope['payload']['api_hash']='c'*32
        self.profile.write_text(json.dumps(envelope))
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.verify.assert_not_awaited();self.assertEqual(self.env.read_bytes(),self.original)

    def test_new_installation_does_not_borrow_a_user_session(self):
        self.session.unlink();self.env.unlink()
        self.migrate();self.verify.assert_not_awaited()
        values=migration.env_values(self.env.read_text())
        self.assertEqual(set(values),set(migration.API_KEYS))
        self.assertFalse(self.session.exists())

    def test_existing_session_without_old_pair_is_not_blindly_replaced(self):
        self.env.write_text('SESSION_NAME=sender_007\nMANAGER_ID=manager_007\n')
        before=self.env.read_bytes()
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.assertEqual(self.env.read_bytes(),before)

    def test_repeated_start_is_idempotent_and_does_not_repeat_probe(self):
        self.migrate();before=self.env.read_bytes();marker=(self.root/'data/telegram-app-migration.json').read_bytes()
        result=self.migrate();self.assertEqual(result['status'],'already_current')
        self.verify.assert_awaited_once();self.assertEqual(self.env.read_bytes(),before)
        self.assertEqual((self.root/'data/telegram-app-migration.json').read_bytes(),marker)

    def test_validly_signed_older_profile_does_not_downgrade(self):
        self.migrate();self.payload.update(version=1,api_id=345678,api_hash='c'*32);self.sign()
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.assertEqual(migration.env_values(self.env.read_text())['TELEGRAM_API_HASH'],self.new[1])

    def test_matching_profile_is_also_remembered_for_anti_downgrade(self):
        self.env.write_bytes(self.original.replace(str(self.old[0]).encode(),str(self.new[0]).encode()).replace(self.old[1].encode(),self.new[1].encode()))
        self.migrate();self.payload.update(version=1,api_id=self.old[0],api_hash=self.old[1]);self.sign()
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.verify.assert_not_awaited()

    def test_rollback_restores_old_pair_and_never_resumes_a_queue(self):
        self.migrate();queue=self.queue.read_bytes()
        self.assertEqual(migration.rollback(self.root)['status'],'rolled_back')
        self.assertEqual(self.env.read_bytes(),self.original);self.assertEqual(self.queue.read_bytes(),queue)
        with self.assertRaises(migration.MigrationDeferred):self.migrate()

    def test_rollback_refuses_to_overwrite_user_changes(self):
        self.migrate();self.env.write_bytes(self.env.read_bytes()+b'OTHER_SETTING=changed\n')
        before=self.env.read_bytes()
        with self.assertRaises(migration.MigrationDeferred):migration.rollback(self.root)
        self.assertEqual(self.env.read_bytes(),before)

    def test_running_sender_or_updater_prevents_migration(self):
        for name in ('sender.lock','startup.lock'):
            with migration._one_lock(self.root/'data'/name):
                with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.verify.assert_not_awaited();self.assertEqual(self.env.read_bytes(),self.original)

    def test_duplicate_env_keys_refuse_ambiguous_change(self):
        self.env.write_bytes(self.original+b'TELEGRAM_API_ID=999999\r\n')
        with self.assertRaises(migration.MigrationDeferred):self.migrate()
        self.verify.assert_not_awaited()

    def test_absent_profile_and_self_check_make_no_changes(self):
        before=sorted(p.name for p in self.root.iterdir())
        self.assertEqual(migration.apply_bundled_profile(self.root)['status'],'not_requested')
        self.assertEqual(migration.apply_bundled_profile(self.root,offline=True)['status'],'not_requested')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),before)

    def test_missing_trust_file_preserves_previous_working_configuration(self):
        (self.root/'telegram-app-profile.private.json').write_bytes(self.profile.read_bytes())
        self.assertEqual(migration.apply_bundled_profile(self.root)['status'],'deferred')
        self.assertEqual(self.env.read_bytes(),self.original)

    def session_fixture(self):
        self.session.unlink()
        with sqlite3.connect(self.session) as db:
            db.execute('CREATE TABLE sessions(dc_id,server_address,port,auth_key)')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(2,'127.0.0.1',443,b'x'*256))

    def client_fixture(self,account=42,authorized=True):
        return Object(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=authorized),get_me=AsyncMock(return_value=Object(id=account)))

    def test_real_probe_adapter_uses_only_in_memory_session_copies_and_read_requests(self):
        self.session_fixture();before=self.session.read_bytes()
        clients=[self.client_fixture(),self.client_fixture()]
        with patch('safe_telegram.ProtectedTelegramClient',side_effect=clients) as factory:
            asyncio.run(migration.probe_existing_session(self.root,migration.env_values(self.original.decode()),self.old,self.new))
        self.assertEqual(self.session.read_bytes(),before)
        from telethon.sessions import MemorySession
        self.assertEqual(factory.call_count,2)
        for call in factory.call_args_list:
            self.assertIsInstance(call.args[0],MemorySession)
            self.assertFalse(call.kwargs['receive_updates'])
        self.assertEqual([call.args[1:] for call in factory.call_args_list],[self.old,self.new])
        for client in clients:client.disconnect.assert_awaited_once()

    def test_real_probe_rejects_a_different_account(self):
        self.session_fixture();before=self.session.read_bytes()
        with patch('safe_telegram.ProtectedTelegramClient',side_effect=[self.client_fixture(42),self.client_fixture(43)]):
            with self.assertRaises(migration.MigrationDeferred):
                asyncio.run(migration.probe_existing_session(self.root,migration.env_values(self.original.decode()),self.old,self.new))
        self.assertEqual(self.session.read_bytes(),before)

    def test_real_probe_does_not_request_login_codes_when_session_is_unauthorized(self):
        self.session_fixture();client=self.client_fixture(authorized=False)
        with patch('safe_telegram.ProtectedTelegramClient',return_value=client) as factory:
            with self.assertRaises(migration.MigrationDeferred):
                asyncio.run(migration.probe_existing_session(self.root,migration.env_values(self.original.decode()),self.old,self.new))
        factory.assert_called_once();client.get_me.assert_not_awaited();client.disconnect.assert_awaited_once()


if __name__=='__main__':unittest.main(verbosity=2)
