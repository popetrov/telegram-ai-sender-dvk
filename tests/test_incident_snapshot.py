import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from incident_snapshot import snapshot


class IncidentSnapshotTests(unittest.TestCase):
    def test_offline_report_never_executes_sender_or_discloses_credentials_or_customer_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            (root/'.env').write_text('MANAGER_ID=manager_004\nBOT_TOKEN=SECRET_TOKEN\nSESSION_NAME=private\n',encoding='utf-8')
            (root/'private.session').write_bytes(b'SECRET_AUTH_KEY')
            (root/'main.py').write_text("raise RuntimeError('MUST_NOT_EXECUTE')\n",encoding='utf-8')
            (root/'access_runtime.py').write_text('async def clean(c):\n await c.delete_messages(1,[2])\n',encoding='utf-8')
            (root/'data/telegram-app-binding.private.json').write_text(json.dumps(dict(payload=dict(
                account_id=42,manager_id='manager_004',profile='SECRET_API_HASH'))),encoding='utf-8')
            db=sqlite3.connect(root/'data/local.sqlite')
            db.execute('CREATE TABLE clients(name TEXT)');db.execute("INSERT INTO clients VALUES ('PRIVATE_CUSTOMER')")
            db.commit();db.close()
            before={p.relative_to(root):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            result=snapshot(root);text=json.dumps(result)
            after={p.relative_to(root):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            for value in ('SECRET_TOKEN','SECRET_AUTH_KEY','SECRET_API_HASH','PRIVATE_CUSTOMER','MUST_NOT_EXECUTE'):
                self.assertNotIn(value,text)
            item=result['installations'][0]
            self.assertEqual(item['database_counts']['clients'],1)
            self.assertEqual(item['files']['access_runtime.py']['history_related_calls'][0]['operation'],'delete_messages')
            self.assertTrue(item['binding_manager_matches_env'])

    def test_pointer_traversal_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            (root/'data/active-installation.json').write_text('{"directory":"../private"}',encoding='utf-8')
            result=snapshot(root)
            self.assertIn('pointer_error_type',result)
            self.assertEqual(len(result['installations']),1)
