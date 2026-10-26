import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_security_fix as builder
import install_security_fix as installer
from file_lock import FileLock

ROOT=Path(__file__).resolve().parents[1]


class SecurityInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload=builder.payload(ROOT)
        cls.old={}
        for name in installer.FILES:
            result=subprocess.run(['git','show','v2.0.27:'+name],cwd=ROOT,capture_output=True)
            if result.returncode==0:cls.old[name]=result.stdout

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.root=self.folder/'sender';self.root.mkdir();self.fixture(self.root)

    def fixture(self,root):
        (root/'data').mkdir()
        for name,raw in self.old.items():(root/name).write_bytes(raw)
        (root/'data/installed-baseline.json').write_text(json.dumps({n:r.decode('utf-8') for n,r in self.old.items()}),encoding='utf-8')
        (root/'.env').write_bytes(b'MANAGER_ID=manager_004\nSESSION_NAME=private\nBOT_TOKEN=PRIVATE_TEST_TOKEN\n')
        (root/'private.session').write_bytes(b'PRIVATE_TEST_SESSION')
        db=sqlite3.connect(root/'data/local.sqlite')
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,text TEXT,status TEXT)')
        db.execute("INSERT INTO messages VALUES (1,'PRIVATE_CUSTOMER_MESSAGE','prepared')")
        db.commit();db.close()
        (root/'data/schedule.json').write_text('{"state":"interrupted","ids":[1]}',encoding='utf-8')

    def test_active_folder_patch_keeps_all_data_and_pointer_and_does_not_launch_sender(self):
        active=self.folder/(self.root.name+'_dvk_'+'a'*32);active.mkdir();self.fixture(active)
        pointer=self.root/'data/active-installation.json'
        pointer.write_text(json.dumps(dict(format=1,directory=active.name)),encoding='utf-8')
        before=installer.user_data(active);original_pointer=pointer.read_bytes()
        with patch('subprocess.run',side_effect=AssertionError('sender must not start')):
            result=installer.install(self.root,self.payload)
        self.assertEqual(result['status'],'installed')
        self.assertFalse(result['sender_started'])
        self.assertEqual(installer.user_data(active),before)
        self.assertEqual(pointer.read_bytes(),original_pointer)
        self.assertEqual((active/'version.txt').read_text().strip(),'2.0.30')
        baseline=json.loads((active/'data/installed-baseline.json').read_text('utf-8'))
        self.assertEqual(baseline['safe_telegram.py'],(active/'safe_telegram.py').read_bytes().decode('utf-8-sig'))
        self.assertFalse((active/'data/security-install-pending.json').exists())
        self.assertEqual(installer.install(self.root,self.payload)['status'],'already_installed')

    def test_running_sender_refuses_install_before_code_changes(self):
        old=(self.root/'main.py').read_bytes()
        with FileLock(self.root/'data/sender.lock'):
            with self.assertRaises(RuntimeError):installer.install(self.root,self.payload)
        self.assertEqual((self.root/'main.py').read_bytes(),old)

    def test_unknown_source_is_not_overwritten(self):
        path=self.root/'bot.py';raw=path.read_bytes()+b'\n# unreviewed custom code\n';path.write_bytes(raw)
        original_main=(self.root/'main.py').read_bytes()
        with self.assertRaises(ValueError):installer.install(self.root,self.payload)
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual((self.root/'main.py').read_bytes(),original_main)

    def test_partial_install_refuses_startup_and_retry_completes_with_identical_user_data(self):
        before=installer.user_data(self.root)
        atomic=installer.atomic
        def interrupt(path,raw):
            if Path(path).name=='config.py':raise OSError('simulated interruption')
            return atomic(path,raw)
        with patch.object(installer,'atomic',side_effect=interrupt):
            with self.assertRaises(OSError):installer.install(self.root,self.payload)
        result=subprocess.run([sys.executable,'main.py'],cwd=self.root,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(installer.user_data(self.root),before)
        installer.install(self.root,self.payload)
        self.assertEqual(installer.user_data(self.root),before)
        self.assertFalse((self.root/'data/security-install-pending.json').exists())

    def test_crash_after_runtime_write_still_has_main_level_stop_and_resumes(self):
        atomic=installer.atomic
        def interrupt(path,raw):
            if Path(path).name=='security-patch.json':raise OSError('simulated power loss')
            return atomic(path,raw)
        with patch.object(installer,'atomic',side_effect=interrupt):
            with self.assertRaises(OSError):installer.install(self.root,self.payload)
        result=subprocess.run([sys.executable,'main.py'],cwd=self.root,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn(b'ModuleNotFoundError',result.stderr)
        installer.install(self.root,self.payload)
        self.assertFalse((self.root/'data/security-install-pending.json').exists())

    def test_version_prevents_replacing_security_patch_with_unsafe_stable_28(self):
        installer.install(self.root,self.payload)
        import startup_update
        with patch.object(startup_update,'release_metadata',return_value=dict(tag_name='v2.0.28',draft=False,prerelease=False)):
            self.assertIsNone(startup_update.latest_bundle((self.root/'version.txt').read_text().strip()))
