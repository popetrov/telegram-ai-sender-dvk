import json
import ast
import sys
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
        (root/'data/installed-baseline.json').write_text(json.dumps({'startup_update.py':self.old.decode('utf-8')}),encoding='utf-8')

    def test_repairs_original_and_active_folder_preserving_credentials_and_database(self):
        active=self.folder/(self.root.name+'_dvk_'+'a'*32);active.mkdir();(active/'data').mkdir();self.fixture(active)
        (self.root/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=active.name)))
        result=repair.repair(self.root)
        self.assertEqual(result,active)
        for target in (self.root,active):
            self.assertIn(b'def release_metadata()', (target/'startup_update.py').read_bytes())
            self.assertEqual((target/'.env').read_bytes(),b'MANAGER_ID=manager_007\nAPI_PAIR=unchanged\n')
            self.assertEqual((target/'data/local.sqlite').read_bytes(),b'existing-business-data')
            self.assertEqual(json.loads((target/'data/installed-baseline.json').read_text(encoding='utf-8'))['startup_update.py'],(target/'startup_update.py').read_text(encoding='utf-8'))

    def test_running_program_is_rejected_before_replacing_any_files(self):
        with FileLock(self.root/'data/sender.lock'):
            with self.assertRaises(RuntimeError):repair.repair(self.root)
        self.assertEqual((self.root/'startup_update.py').read_bytes(),self.old)

    def test_unknown_modified_updater_is_not_overwritten(self):
        changed=self.old+b'\n# local change\n';(self.root/'startup_update.py').write_bytes(changed)
        with self.assertRaises(ValueError):repair.repair(self.root)
        self.assertEqual((self.root/'startup_update.py').read_bytes(),changed)

    def test_legacy_embedded_bridge_uses_fixed_active_updater_after_repair(self):
        active=self.folder/(self.root.name+'_dvk_'+'b'*32);active.mkdir();(active/'data').mkdir();self.fixture(active)
        (self.root/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=active.name)))
        (self.root/'startup_update.py').unlink()
        source=ast.parse(subprocess.run(['git','show','v2.0.20:build_startup_bridge.py'],check=True,capture_output=True).stdout)
        template=next(n.value.left.value for n in ast.walk(source) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='script' for t in n.targets))
        old=(template%(b'placeholder','placeholder')).encode()
        self.assertTrue(repair.known_bridge(old))
        for name in ('launcher.py','main.py'):(self.root/name).write_bytes(old)
        repair.repair(self.root)
        self.assertEqual((self.root/'launcher.py').read_bytes(),repair.BRIDGE_REPAIRED)
        # Prove the old shortcut loads the active updater, not its frozen payload.
        (active/'startup_update.py').write_text('from pathlib import Path\ndef run(root,**kwargs):\n (Path(root)/"selected.txt").write_text(__file__)\n return 0\n')
        subprocess.run([sys.executable,str(self.root/'launcher.py')],check=True,cwd=self.root)
        self.assertEqual(Path((self.root/'selected.txt').read_text()),active/'startup_update.py')


if __name__=='__main__':unittest.main()
