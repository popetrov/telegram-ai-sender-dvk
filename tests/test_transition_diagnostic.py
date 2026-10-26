import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import diagnose_legacy_transition as diagnostic

class TransitionDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'sender';self.root.mkdir()
        (self.root/'version.txt').write_text('2.0.27')
        for name in ('.env','manager.session','clients.sqlite'):(self.root/name).write_bytes(b'secret-canary-do-not-open')
    def check(self):
        original=diagnostic.read
        def guarded(path,*args):
            self.assertNotIn(Path(path).name,{'.env','manager.session','clients.sqlite'})
            return original(path,*args)
        with patch('diagnose_legacy_transition.read',side_effect=guarded):return '\n'.join(diagnostic.inspect(self.root))
    def test_missing_backup_is_diagnosed_without_sensitive_file_reads_or_writes(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        report=self.check();self.assertIn('отсутствует резервная копия',report)
        self.assertNotIn('secret-canary',report);self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
    def test_renamed_folder_pointer_is_explained(self):
        data=self.root/'data';data.mkdir();(data/'active-installation.json').write_text(json.dumps(dict(format=1,directory='old_name_dvk_'+'a'*32)))
        self.assertIn('переименование или копирование',self.check())
    def test_missing_active_sibling_is_explained(self):
        data=self.root/'data';data.mkdir();(data/'active-installation.json').write_text(json.dumps(dict(format=1,directory='sender_dvk_'+'a'*32)))
        self.assertIn('папка активной версии отсутствует',self.check())
    def test_missing_backup_file_list_is_explained(self):
        backup=self.root/'data/backups/20260101';backup.mkdir(parents=True)
        (backup/'manifest.json').write_text(json.dumps(dict(existing_files=['main.py'])))
        self.assertIn('состав последней резервной копии',self.check())
