from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import center_setup


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.startup = self.root / 'Microsoft/Windows/Start Menu/Programs/Startup'
        self.startup.mkdir(parents=True)
        self.target = self.startup / 'DVK Center.cmd'

    def test_repeated_enable_then_disable_handles_windows_line_endings(self):
        with patch.object(center_setup,'os',SimpleNamespace(name='nt',environ={'APPDATA':str(self.root)})), \
             patch.object(center_setup,'ROOT',self.root / 'center'):
            center_setup.startup(True)
            content = self.target.read_bytes()
            self.assertIn(b'\r\n',content)
            self.assertNotIn(b'\r\r\n',content)
            center_setup.startup(True)
            self.assertEqual(self.target.read_bytes(),content)
            center_setup.startup(False)
            self.assertFalse(self.target.exists())

    def test_foreign_startup_is_not_overwritten(self):
        self.target.write_text('another installation\n',encoding='utf-8')
        with patch.object(center_setup,'os',SimpleNamespace(name='nt',environ={'APPDATA':str(self.root)})):
            with self.assertRaises(RuntimeError):
                center_setup.startup(True)
        self.assertEqual(self.target.read_text('utf-8'),'another installation\n')
