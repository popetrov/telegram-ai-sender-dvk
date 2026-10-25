import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from build_platform_release import build_transition, ROOT
from manual_update import build_package
from manager_setup import configure_center
from transition_setup import verify_bundle,prepare_transition,finish_transition
from transition_fixture import create_fixture
from dotenv import dotenv_values


class TransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.shared_root = Path(cls.shared.name)
        manager = cls.shared_root/'manager.zip'
        build_package(ROOT,manager)
        package = cls.shared_root/'transition.zip'
        build_transition(ROOT,package,manager)
        cls.bundle = cls.shared_root/'bundle'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(cls.bundle)
        cls.baselines = json.loads((cls.bundle/'legacy-baselines.json').read_text('utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = create_fixture(self.baselines,self.root/'legacy')
        self.destination = self.root/'new'

    def hashes(self):
        return {p.relative_to(self.source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.source.rglob('*') if p.is_file() and p.name!='sender.lock' and '__pycache__' not in p.parts}

    def test_actual_legacy_source_migrates_without_git_and_keeps_source_and_session(self):
        before = self.hashes()
        report = prepare_transition(self.source,self.destination,self.bundle)
        self.assertEqual(report['conflicts'],[])
        self.assertEqual(self.hashes(),before)
        self.assertFalse(report['activated'])
        self.assertEqual((self.destination/'version.txt').read_text('utf-8').strip(),(ROOT/'version.txt').read_text('utf-8-sig').strip())
        self.assertEqual((self.destination/'.env').read_bytes(),(self.source/'.env').read_bytes())
        with sqlite3.connect(self.destination/'fixture_session.session') as db:
            self.assertEqual(db.execute('SELECT value FROM fixture_data').fetchone()[0],'synthetic session marker')
        db.close()

    def test_custom_products_prompts_and_paused_draft_are_preserved(self):
        products_path = self.source/'products.json'
        products = json.loads(products_path.read_text('utf-8'))
        products['Custom'] = dict(prompt='custom.md',aliases=['custom'])
        products_path.write_text(json.dumps(products),encoding='utf-8')
        (self.source/'prompts/custom.md').write_text('Custom prompt',encoding='utf-8')
        (self.source/'prompts/khimikaty_dlya_rasteniy.md').write_text('Paused draft',encoding='utf-8')
        report = prepare_transition(self.source,self.destination,self.bundle)
        self.assertEqual(report['conflicts'],[])
        merged = json.loads((self.destination/'products.json').read_text('utf-8'))
        self.assertIn('Custom',merged)
        self.assertEqual((self.destination/'prompts/custom.md').read_text('utf-8'),'Custom prompt')
        self.assertFalse(any(p.get('prompt')=='khimikaty_dlya_rasteniy.md' for p in merged.values()))

    def test_unknown_core_change_is_rejected_before_candidate_creation(self):
        with (self.source/'main.py').open('a',encoding='utf-8') as stream:
            stream.write('\n# local core modification\n')
        before = self.hashes()
        with self.assertRaisesRegex(ValueError,'main.py'):
            prepare_transition(self.source,self.destination,self.bundle)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.hashes(),before)

    def test_corrupt_bundle_stops_before_candidate_creation(self):
        import shutil
        bundle = self.root/'corrupt'
        shutil.copytree(self.bundle,bundle)
        with (bundle/'manager-package.zip').open('ab') as stream:
            stream.write(b'corrupt')
        with self.assertRaises(ValueError):
            prepare_transition(self.source,self.destination,bundle)
        self.assertFalse(self.destination.exists())

    def test_public_setup_preserves_manager_credentials_and_refuses_key_rotation(self):
        prepare_transition(self.source,self.destination,self.bundle)
        path = self.destination/'.env'
        original = dotenv_values(path)
        public = json.loads((self.bundle/'center-public.json').read_text('utf-8'))
        configure_center(path,public)
        updated = dotenv_values(path)
        for name in ('MANAGER_ID','SESSION_NAME','OPENAI_API_KEY','BOT_TOKEN','TELEGRAM_API_HASH'):
            self.assertEqual(updated[name],original[name])
        public['access_public_key'] = __import__('base64').b64encode(b'x'*32).decode()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            configure_center(path,public)
        self.assertEqual(path.read_bytes(),before)

    def test_actual_legacy_database_and_catalog_pass_new_local_startup_probe(self):
        prepare_transition(self.source,self.destination,self.bundle)
        public = json.loads((self.bundle/'center-public.json').read_text('utf-8'))
        configure_center(self.destination/'.env',public)
        result = subprocess.run([sys.executable,'main.py','--self-check'],cwd=self.destination,
                                capture_output=True,timeout=60,
                                env={**os.environ,**dict(dotenv_values(self.destination/'.env'))})
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    def test_failed_dependency_install_leaves_original_and_readiness_unchanged(self):
        prepare_transition(self.source,self.destination,self.bundle)
        before = self.hashes()
        with patch('transition_setup.subprocess.run',side_effect=subprocess.CalledProcessError(1,['synthetic-pip'])):
            with self.assertRaises(subprocess.CalledProcessError):
                finish_transition(self.destination,self.bundle)
        report = json.loads((self.destination/'transition-report.json').read_text('utf-8'))
        self.assertFalse(report['ready_for_pilot'])
        self.assertFalse(report['activated'])
        self.assertEqual(self.hashes(),before)

    def test_existing_destination_is_not_overwritten(self):
        self.destination.mkdir()
        marker = self.destination/'keep.txt'
        marker.write_text('keep',encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare_transition(self.source,self.destination,self.bundle)
        self.assertEqual(marker.read_text('utf-8'),'keep')
