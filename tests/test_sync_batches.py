import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

for name,value in dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='test',OPENAI_API_KEY='test',
                       BOT_TOKEN='123:test',MANAGER_ID='manager').items():
    os.environ.setdefault(name,value)
from authority_store import AuthorityStore
from sync_data import enqueue, next_transfer
from sync_protocol import (MAX_BATCH_BYTES, MAX_BATCH_RECORDS, batch_members, event_digest,
                           MAX_BATCH_PLAIN_BYTES, MAX_COMPRESSED_PLAIN_BYTES, decompress_bounded,
                           encrypt, decrypt, receipt, public_text, event_parts)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import asyncio
import sync_runtime


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=AuthorityStore(Path(self.temp.name)/'center.sqlite')
        self.store.register(42,'manager:42')
        self.store.assign(42,'dvk')
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE local_settings(key TEXT,value TEXT);"
                             "INSERT INTO local_settings VALUES ('sync_installation_id','installation');"
                             "CREATE TABLE sync_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,payload TEXT);")

    def add(self,n=3,text='text'):
        for index in range(n):
            enqueue(self.db,dict(table='messages',row=dict(id=index,text=text,status='sent')))
        self.db.commit()

    def count(self):
        with self.store.connection() as db:
            return db.execute('SELECT count(*) FROM events').fetchone()[0]

    def receive(self,transfer):
        ids,event_id,data=transfer
        self.store.receive(42,'manager:42',event_id,event_digest(data),data)

    def test_batch_has_bounded_size_and_count_and_keeps_oversized_single_path(self):
        self.add(MAX_BATCH_RECORDS+3)
        ids,wire_id,data=next_transfer(self.db)
        self.assertEqual(len(ids),MAX_BATCH_RECORDS)
        self.assertEqual(len(batch_members(data)),MAX_BATCH_RECORDS)
        self.assertEqual(wire_id,event_digest(data)[:32])
        self.db.execute('DELETE FROM sync_outbox')
        self.add(2,'x'*(MAX_BATCH_PLAIN_BYTES//2))
        ids,wire_id,data=next_transfer(self.db)
        self.assertEqual(len(ids),1)
        self.assertEqual(data['data']['table'],'messages')
        self.db.execute('DELETE FROM sync_outbox')
        self.add(1,'x'*(MAX_BATCH_BYTES*3))
        ids,wire_id,data=next_transfer(self.db)
        self.assertGreater(len(event_parts(wire_id,data)),1)

    def test_encrypted_batch_keeps_original_ids_digests_ownership_and_retry_is_idempotent(self):
        self.add()
        ids,wire_id,data=next_transfer(self.db)
        encryption=X25519PrivateKey.generate()
        packet=encrypt(public_text(encryption),dict(subject='manager:42',event_id=wire_id,data=data))
        restored=decrypt(encryption,packet)
        for _ in range(2):
            self.store.receive(42,restored['subject'],restored['event_id'],event_digest(restored['data']),restored['data'])
        self.assertEqual(self.count(),len(ids))
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM events').fetchall()
        self.assertEqual({r['event_id'] for r in rows},set(ids))
        for r in rows:
            self.assertEqual(r['company_id'],'dvk')
            self.assertEqual(r['subject'],'manager:42')
            self.assertEqual(r['digest'],event_digest(json.loads(r['payload'])))

    def test_large_compressible_batch_uses_one_small_authenticated_packet(self):
        self.add(100, 'analysis paragraph ' * 1500)
        ids, wire_id, data = next_transfer(self.db)
        self.assertEqual(len(ids), 100)
        self.assertEqual(len(event_parts(wire_id, data)), 1)
        key = X25519PrivateKey.generate()
        payload = dict(subject='manager:42', event_id=wire_id, data=data)
        packet = encrypt(public_text(key), payload, compress=True)
        self.assertEqual(json.loads(packet)['version'], 2)
        self.assertLess(len(packet), MAX_BATCH_BYTES)
        self.assertEqual(decrypt(key, packet), payload)
        self.receive((ids, wire_id, data))
        self.assertEqual(self.count(), 100)
        wrapper = json.loads(packet)
        wrapper['version'] = 1
        with self.assertRaises(Exception):
            decrypt(key, json.dumps(wrapper).encode())

    def test_decompression_rejects_bombs_truncation_and_trailing_data(self):
        for content in (zlib.compress(b'x' * (MAX_COMPRESSED_PLAIN_BYTES + 1)),
                        zlib.compress(b'{}')[:-1], zlib.compress(b'{}') + b'trailing'):
            with self.assertRaises(ValueError):
                decompress_bounded(content)

    def test_incompressible_batches_shrink_without_losing_events(self):
        self.add(20, base64.b64encode(os.urandom(70000)).decode())
        ids, wire_id, data = next_transfer(self.db)
        self.assertGreater(len(ids), 1)
        self.assertLess(len(ids), 20)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0], 20)
        key = X25519PrivateKey.generate()
        packet = encrypt(public_text(key), dict(subject='manager:42',event_id=wire_id,data=data), compress=True)
        self.assertEqual(decrypt(key, packet)['data'], data)

    def test_conflicting_member_rolls_back_entire_batch(self):
        self.add()
        ids,wire_id,data=next_transfer(self.db)
        member=dict(installation='installation',data=data['data']['row']['events'][-1]['data'])
        self.store.receive(42,'manager:42',ids[-1],'existing-conflict',member)
        with self.assertRaises(ValueError):
            self.receive((ids,wire_id,data))
        self.assertEqual(self.count(),1)

    def test_invalid_nested_or_duplicate_member_and_unbound_identity_are_rejected(self):
        self.add()
        transfer=next_transfer(self.db)
        ids,wire_id,data=transfer
        with self.assertRaises(ValueError):
            self.store.receive(43,'manager:42',wire_id,event_digest(data),data)
        events=data['data']['row']['events']
        events[1]['event_id']=events[0]['event_id']
        with self.assertRaises(ValueError):
            batch_members(data)
        events[1]['event_id']='f'*32
        events[1]['data']['table']='sync_batch'
        with self.assertRaises(ValueError):
            batch_members(data)
        self.assertEqual(self.count(),0)

    async def run_sender(self,lose_receipt=False,append_after_upload=False):
        signing=Ed25519PrivateKey.generate()
        encryption=X25519PrivateKey.generate()
        state={}
        async def upload(authority,stream,**kwargs):
            payload=decrypt(encryption,stream.getvalue())
            data=payload['data']
            self.store.receive(42,payload['subject'],payload['event_id'],event_digest(data),data)
            state['ack']='DVK_RECEIPT '+json.dumps(receipt(signing,payload['subject'],payload['event_id'],event_digest(data)))
            if append_after_upload:
                self.add(1,'new after batch snapshot')
            return SimpleNamespace(id=1)
        async def response(*args):
            if lose_receipt:
                raise TimeoutError('receipt lost')
            return SimpleNamespace(raw_text=state['ack'])
        conversation=SimpleNamespace(get_response=response)
        class Context:
            async def __aenter__(self): return conversation
            async def __aexit__(self,*args): return False
        client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
                               send_file=upload,conversation=lambda *a,**kw:Context())
        @contextmanager
        def connection():
            with self.db:
                yield self.db
        sleeps=0
        async def sleep(seconds):
            nonlocal sleeps
            sleeps+=1
            if lose_receipt or sleeps>=2:
                raise asyncio.CancelledError
        environment=dict(ACCESS_BOT_USERNAME='service_bot',SYNC_PUBLIC_KEY=public_text(encryption),
                         ACCESS_PUBLIC_KEY=base64.b64encode(signing.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode(),
                         MANAGER_ID='manager')
        with patch.dict(os.environ,environment), patch('database.get_connection',connection), \
             patch('sync_runtime.cleanup_service_messages',new_callable=AsyncMock), \
             patch('sync_runtime.asyncio.sleep',side_effect=sleep):
            task=await sync_runtime.start(client)
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_lost_receipt_keeps_outbox_and_retry_does_not_duplicate_center(self):
        self.add()
        await self.run_sender(lose_receipt=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],3)
        self.assertEqual(self.count(),3)
        await self.run_sender()
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],0)
        self.assertEqual(self.count(),3)

    async def test_ack_removes_only_the_confirmed_snapshot_not_new_events(self):
        self.add()
        await self.run_sender(append_after_upload=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_outbox').fetchone()[0],1)
        self.assertEqual(self.count(),3)
