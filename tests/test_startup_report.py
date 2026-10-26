import asyncio,json,tempfile,unittest,urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import startup_report as report
class StartupReportTests(unittest.TestCase):
    def test_secret_text_from_network_errors_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.txt'
            with patch.object(report,'REPORT',path):report.record('network',urllib.error.URLError('private-proxy-password'))
            entry=json.loads(path.read_text('utf-8'));self.assertEqual(entry['error_type'],'URLError')
            self.assertNotIn('private-proxy-password',path.read_text('utf-8'))
    def test_sync_hook_keeps_result_and_records_no_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.txt';module=SimpleNamespace(call=lambda secret:secret)
            with patch.object(report,'REPORT',path):
                report.wrap(module,'call','test');self.assertEqual(module.call('token-canary'),'token-canary')
            self.assertNotIn('token-canary',path.read_text('utf-8'))
    def test_async_hook_preserves_exception(self):
        async def fail(value):raise TimeoutError(value)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.txt';module=SimpleNamespace(call=fail)
            with patch.object(report,'REPORT',path):
                report.wrap(module,'call','test')
                with self.assertRaises(TimeoutError):asyncio.run(module.call('secret-canary'))
            self.assertIn('TimeoutError',path.read_text('utf-8'));self.assertNotIn('secret-canary',path.read_text('utf-8'))
