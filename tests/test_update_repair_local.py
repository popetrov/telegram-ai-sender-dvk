import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from file_lock import FileLock
import repair_update_local as repair


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();self.root=self.folder/'application';self.root.mkdir();(self.root/'data').mkdir()
        self.old=subprocess.run(['git','show','v2.0.20:startup_update.py'],check=True,capture_output=True).stdout
        self.fixture(self.root)

    def fixture(self,root):
        (root/'startup_update.py').write_bytes(self.old)
        (root/'.env').write_bytes(b'MANAGER_ID=manager_007\nAPI_PAIR=unchanged\n')
        (root/'data/local.sqlite').write_bytes(b'existing-business-data')
        (root/'data/installed-baseline.json').write_text(json.dumps({'startup_update.py':self.old.decode('utf-8')}))

    def test_repairs_original_and_active_folder_preserving_credentials_and_database(self):
        active=self.folder/(self.root.name+'_dvk_'+'a'*32);active.mkdir();(active/'data').mkdir();self.fixture(active)
        (self.root/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=active.name)))
        result=repair.repair(self.root)
        self.assertEqual(result,active)
        for target in (self.root,active):
            self.assertIn(b'def release_metadata()', (target/'startup_update.py').read_bytes())
            self.assertEqual((target/'.env').read_bytes(),b'MANAGER_ID=manager_007\nAPI_PAIR=unchanged\n')
            self.assertEqual((target/'data/local.sqlite').read_bytes(),b'existing-business-data')
            self.assertEqual(json.loads((target/'data/installed-baseline.json').read_text())['startup_update.py'],(target/'startup_update.py').read_text())

    def test_running_program_is_rejected_before_replacing_any_files(self):
        with FileLock(self.root/'data/sender.lock'):
            with self.assertRaises(RuntimeError):repair.repair(self.root)
        self.assertEqual((self.root/'startup_update.py').read_bytes(),self.old)

    def test_unknown_modified_updater_is_not_overwritten(self):
        changed=self.old+b'\n# local change\n';(self.root/'startup_update.py').write_bytes(changed)
        with self.assertRaises(ValueError):repair.repair(self.root)
        self.assertEqual((self.root/'startup_update.py').read_bytes(),changed)


if __name__=='__main__':unittest.main()
