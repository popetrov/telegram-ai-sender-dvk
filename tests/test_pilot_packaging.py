import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_platform_release import build_center, CENTER_FILES, ROOT
from file_lock import FileLock
from manual_update import build_package, verify_package
from platform_updater import stage


class PilotPackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_center_archive_contains_only_allowlisted_files(self):
        output = self.root / 'center.zip'
        digest = build_center(ROOT, output)
        self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest())
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), CENTER_FILES | {'manifest.json'})
            self.assertFalse(any(name.endswith(('.key','.sqlite','.session')) for name in archive.namelist()))
            self.assertNotIn('.env.authority', archive.namelist())
            self.assertNotIn('bot.py', archive.namelist())
            manifest = json.loads(archive.read('manifest.json'))
            for name, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected)

    def test_center_package_check_works_after_extraction(self):
        output = self.root / 'center.zip'
        build_center(ROOT, output)
        with zipfile.ZipFile(output) as archive:
            archive.extractall(self.root / 'center')
        result = subprocess.run([sys.executable, 'center_setup.py', 'check'], cwd=self.root / 'center',
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_manager_package_verifies_and_never_contains_authority_secrets(self):
        output = self.root / 'manager.zip'
        digest = build_package(ROOT, output)
        manifest, payload = verify_package(output, digest)
        self.assertIn('sync_runtime.py', payload)
        self.assertIn('platform_updater.py', payload)
        self.assertNotIn('access_server.py', payload)
        self.assertFalse(any(name.startswith('data/') or name == '.env' for name in payload))

    def test_update_refuses_running_sender(self):
        source = self.root / 'source'
        source.mkdir()
        with FileLock(source / 'data/sender.lock'):
            with self.assertRaises(RuntimeError):
                stage(source, self.root / 'absent.zip', 'a'*64, self.root / 'candidate')
        self.assertFalse((self.root / 'candidate').exists())

    def test_separate_candidate_preserves_owner_files_and_runs_local_probe(self):
        source = self.root / 'owner'
        source.mkdir()
        (source / 'version.txt').write_text('2.0.9', encoding='utf-8')
        (source / '.env').write_text('TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=test\n'
                                    'OPENAI_API_KEY=test\nBOT_TOKEN=123:test\nMANAGER_ID=owner\n', encoding='utf-8')
        (source / 'prompts').mkdir()
        (source / 'prompts/bg.md').write_text('LOCAL OWNER PROMPT', encoding='utf-8')
        (source / 'prompts/khimikaty_dlya_rasteniy.md').write_text('PAUSED DRAFT', encoding='utf-8')
        original = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        package = self.root / 'manager.zip'
        digest = build_package(ROOT, package)
        candidate = self.root / 'candidate'
        report = stage(source, package, digest, candidate)
        self.assertIn('prompts/bg.md', report['conflicts'])
        for name, data in original.items():
            self.assertEqual((source / name).read_bytes(), data)
        self.assertEqual((candidate / 'prompts/bg.md').read_text('utf-8'), 'LOCAL OWNER PROMPT')
        self.assertEqual((candidate / 'prompts/khimikaty_dlya_rasteniy.md').read_text('utf-8'), 'PAUSED DRAFT')
        products = json.loads((candidate / 'products.json').read_text('utf-8'))
        self.assertFalse(any('khimikaty' in p.get('prompt','') for p in products.values()))
        result = subprocess.run([sys.executable, 'main.py', '--self-check'], cwd=candidate,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_pilot_start_does_not_use_legacy_updater(self):
        from unittest.mock import patch
        import start
        with patch('start.prepare_environment', return_value=Path(sys.executable)), \
             patch('start.command_status', return_value=0) as command:
            self.assertEqual(start.main([]), 0)
            self.assertTrue(str(command.call_args.args[0][-1]).endswith('main.py'))
