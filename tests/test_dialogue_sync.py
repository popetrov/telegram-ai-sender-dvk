import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dialogue_store import DialogueStore,require_contact_allowed,ContactOptOutError
from sync_data import initialize
from sync_protocol import event_digest
from authority_store import AuthorityStore


class DialogueSyncTests(unittest.TestCase):
    def test_dialogue_events_share_existing_transactional_outbox_and_owner_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'local.sqlite'
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE local_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
            initialize(db);db.commit();db.close()
            store=DialogueStore(path,clock=lambda:1000);store.initialize(sync=True)
            thread=store.enroll(company_id='dvk',business_id='dvk_finance',manager_id='manager_001',account_id=42,peer_id=77,product='БГ',first_message_id=10,first_sent_at=100)
            with store.connection() as db:
                row=db.execute('SELECT * FROM sync_outbox ORDER BY sequence DESC LIMIT 1').fetchone()
                installation=db.execute("SELECT value FROM local_settings WHERE key='sync_installation_id'").fetchone()[0]
                payload=dict(installation=installation,data=json.loads(row['payload']))
                self.assertEqual(payload['data']['table'],'dialogue_threads')
            center=AuthorityStore(root/'center.sqlite');center.register(42,'manager_001:42');center.assign(42,'dvk')
            center.receive(42,'manager_001:42',row['event_id'],event_digest(payload),payload)
            with center.connection() as db:
                received=db.execute('SELECT company_id,subject FROM events').fetchone()
                self.assertEqual(tuple(received),('dvk','manager_001:42'))
            store.set_state(thread,'opt_out',expected_revision=0)
            with self.assertRaises(ContactOptOutError):require_contact_allowed(path,manager_id='manager_001',peer_id=77)
            require_contact_allowed(path,manager_id='manager_001',peer_id=78)
            require_contact_allowed(path,manager_id='manager_002',peer_id=77)
