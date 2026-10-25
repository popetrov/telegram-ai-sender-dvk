import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch
from dialogue_setup import configure
from dialogue_send_gate import DeliveryGate


class SetupGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        (self.root/'.env').write_text('MANAGER_ID=manager_001\n',encoding='utf-8')
        with closing(sqlite3.connect(self.root/'data/local.sqlite')) as db:
            db.executescript('''CREATE TABLE clients(id INTEGER,manager_id TEXT,username TEXT,telegram_id INTEGER,product TEXT);
                CREATE TABLE messages(client_id INTEGER,status TEXT,telegram_message_id INTEGER);
                INSERT INTO clients VALUES(1,'manager_001','tester',77,'БГ');
                INSERT INTO messages VALUES(1,'sent',100);''')

    def test_single_known_previously_contacted_peer_is_enabled_without_network(self):
        result=configure(self.root,username='@TESTER',product='БГ')
        self.assertEqual(result['peer_id'],77)
        self.assertEqual(result['mode'],'pilot')
        self.assertEqual(json.loads((self.root/'data/dialogue-runtime.json').read_text('utf-8')),result)

    def test_unknown_peer_or_wrong_product_does_not_enable_other_chats(self):
        for username,product in [('unknown','БГ'),('tester','Кредит')]:
            with self.assertRaises(ValueError):configure(self.root,username=username,product=product)
        self.assertFalse((self.root/'data/dialogue-runtime.json').exists())

    async def test_gate_serializes_both_workers_and_checks_permission_after_pause(self):
        gate=DeliveryGate(interval=10,clock=lambda:100)
        order=[]
        async def first():
            order.append('first-start');await asyncio.sleep(0);order.append('first-end')
        async def second():order.append('second')
        guard=Mock()
        await gate.run(first,guard)
        with patch('dialogue_send_gate.asyncio.sleep',new_callable=AsyncMock) as pause:
            await gate.run(second,guard)
        self.assertEqual(order,['first-start','first-end','second'])
        pause.assert_awaited_once_with(10)
        self.assertEqual(guard.call_count,4)

    async def test_revocation_during_rate_pause_cancels_delivery(self):
        gate=DeliveryGate(interval=10,clock=lambda:100);gate.finished=100
        guard=Mock(side_effect=[None,PermissionError('revoked')]);operation=AsyncMock()
        with patch('dialogue_send_gate.asyncio.sleep',new_callable=AsyncMock):
            with self.assertRaises(PermissionError):await gate.run(operation,guard)
        operation.assert_not_awaited()
