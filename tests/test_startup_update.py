import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import zipfile

import startup_update as update
from build_platform_release import ROOT, build_transition
from build_startup_bridge import build as build_bridge
from manual_update import build_package
from transition_fixture import create_fixture
from file_lock import FileLock


class StartupUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.base = Path(cls.shared.name)
        manager = cls.base/'manager.zip'
        build_package(ROOT, manager)
        cls.zip = cls.base/'transition.zip'
        build_transition(ROOT,cls.zip,manager)
        cls.raw = cls.zip.read_bytes()
        cls.bundle = cls.base/'bundle'
        cls.bundle.mkdir()
        update.unpack_bundle(cls.raw, cls.bundle)
        cls.baselines = json.loads((cls.bundle/'legacy-baselines.json').read_text('utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = create_fixture(self.baselines,Path(self.tmp.name)/'old').resolve()
        self.launches = []

    def launch(self, active):
        self.launches.append(active)
        return 0

    def discover(self, current):
        return 'v2.0.20',self.raw

    def test_failed_install_keeps_source_and_launches_previous_version(self):
        before = (self.root/'data/local.sqlite').read_bytes()
        def fail(*args):
            raise RuntimeError('dependency installation failed')
        self.assertEqual(update.run(self.root,discover=self.discover,prepare=fail,launch=self.launch),0)
        self.assertEqual(self.launches,[self.root])
        self.assertFalse((self.root/'data/active-installation.json').exists())
        self.assertEqual((self.root/'data/local.sqlite').read_bytes(),before)

    def test_atomic_activation_next_launch_and_no_rollback_after_sender_error(self):
        def prepare(source,candidate,*args):
            shutil.copytree(source,candidate,ignore=shutil.ignore_patterns('*.lock'))
            (candidate/'version.txt').write_text('2.0.20')
        self.assertEqual(update.run(self.root,discover=self.discover,prepare=prepare,launch=lambda p:7),7)
        active = update.active_installation(self.root)
        self.assertNotEqual(active,self.root)
        with sqlite3.connect(active/'data/local.sqlite') as db:
            db.execute('CREATE TABLE new_sends(id INTEGER)')
        db.close()
        update.run(self.root,updates=False,launch=self.launch)
        self.assertEqual(self.launches,[active])
        with sqlite3.connect(active/'data/local.sqlite') as db:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='new_sends'").fetchone())
        db.close()

    def test_corrupt_pointer_never_falls_back_to_old_database(self):
        (self.root/'data/active-installation.json').write_text('{broken')
        with self.assertRaises(ValueError):
            update.run(self.root,updates=False,launch=self.launch)
        self.assertFalse(self.launches)

    def test_second_start_is_blocked_for_entire_sender_lifetime(self):
        def launch(active):
            with self.assertRaises(RuntimeError):
                update.run(self.root,updates=False,launch=self.launch)
            return 0
        update.run(self.root,updates=False,launch=launch)
        self.assertFalse(self.launches)

    def test_legacy_fallback_reuses_working_interpreter_without_installing_dependencies(self):
        active = self.root.with_name(self.root.name+'_dvk_'+'a'*32)
        shutil.copytree(self.root,active)
        update.activate(self.root,active)
        with patch('start.prepare_environment',side_effect=AssertionError('must not install')), \
             patch('startup_update.subprocess.run',return_value=Mock(returncode=0)) as command:
            self.assertEqual(update.run(self.root,updates=False),0)
            self.assertEqual(command.call_args.args[0],[sys.executable,str(active/'main.py')])

    def test_corrupt_archive_is_rejected_and_previous_version_starts(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:
            archive.writestr('../escape.py','unsafe')
        update.run(self.root,discover=lambda current:('2.0.20',raw.getvalue()),launch=self.launch)
        self.assertEqual(self.launches,[self.root])
        self.assertFalse((self.root.parent/'escape.py').exists())

    def test_only_new_stable_full_release_is_selected(self):
        for extra in ({'prerelease':True},{'draft':True},{'tag_name':'v2.0.19'},{}):
            release = dict(tag_name='v2.0.20',assets=[],**{k:v for k,v in extra.items() if k!='tag_name'})
            release.update(extra)
            with patch.object(update,'fetch',return_value=json.dumps(release).encode()):
                self.assertIsNone(update.latest_bundle('2.0.19'))

    def test_legacy_bridge_matches_old_allowlist_and_preserves_real_local_catalog(self):
        import launcher
        bridge = self.base/'bridge.zip'
        if not bridge.exists():
            build_bridge(ROOT,self.zip,bridge)
        with zipfile.ZipFile(bridge) as archive:
            self.assertEqual(set(archive.namelist()),launcher.UPDATE_ALLOWLIST)
            compile(archive.read('launcher.py'),'launcher.py','exec')
        backup = self.root/'data/backups/20261024_000000'
        backup.mkdir(parents=True)
        for name in update.LEGACY_FILES:
            target = backup/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(self.root/name,target)
        original_products = (self.root/'products.json').read_bytes()
        (backup/'manifest.json').write_text(json.dumps({'existing_files':sorted(update.LEGACY_FILES)}))
        # Simulate the exact overwrites performed by an installed legacy updater.
        with zipfile.ZipFile(bridge) as archive:
            archive.extractall(self.root)
        fallback = update.recover_legacy(self.root,self.bundle)
        self.assertEqual((fallback/'products.json').read_bytes(),original_products)
        self.assertEqual((fallback/'version.txt').read_text().strip(),'2.0.19')
        self.assertEqual((fallback/'.env').read_bytes(),(self.root/'.env').read_bytes())
        self.assertEqual(update.active_installation(self.root),fallback)

    def test_real_preparation_without_network_preserves_data_and_records_baseline(self):
        candidate = self.root.with_name('candidate')
        with FileLock(self.root/'data/sender.lock'), patch('transition_setup.subprocess.run'):
            update.prepare_update(self.root,candidate,self.bundle,'v2.0.20')
        self.assertTrue((candidate/'data/installed-baseline.json').is_file())
        self.assertEqual((candidate/'.env').read_bytes(),(self.root/'.env').read_bytes())
        self.assertEqual(json.loads((candidate/'transition-report.json').read_text())['ready_for_pilot'],True)

    def test_next_update_uses_active_database_and_preserves_custom_prompt_without_git(self):
        import manual_update
        first = self.root.with_name('first')
        with FileLock(self.root/'data/sender.lock'), patch('transition_setup.subprocess.run'):
            update.prepare_update(self.root,first,self.bundle,'v2.0.20')
        (first/'prompts/bg.md').write_text('Local manager text',encoding='utf-8')
        with sqlite3.connect(first/'data/local.sqlite') as db:
            db.execute('CREATE TABLE after_first_update(value TEXT)')
            db.execute("INSERT INTO after_first_update VALUES ('latest local data')")
        db.close()
        package_source = self.root.with_name('release-source')
        package_source.mkdir()
        for name in manual_update.package_files(ROOT):
            target = package_source/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/name,target)
        (package_source/'version.txt').write_text('2.0.21')
        package = self.root.with_name('next-manager.zip')
        build_package(package_source,package)
        bundle_zip = self.root.with_name('next-transition.zip')
        build_transition(ROOT,bundle_zip,package)
        bundle = self.root.with_name('next-bundle')
        bundle.mkdir()
        update.unpack_bundle(bundle_zip.read_bytes(),bundle)
        candidate = self.root.with_name('second')
        with FileLock(first/'data/sender.lock'), patch('transition_setup.subprocess.run'):
            update.prepare_update(first,candidate,bundle,'v2.0.21')
        self.assertEqual((candidate/'prompts/bg.md').read_text('utf-8'),'Local manager text')
        with sqlite3.connect(candidate/'data/local.sqlite') as db:
            self.assertEqual(db.execute('SELECT value FROM after_first_update').fetchone()[0],'latest local data')
        db.close()
        self.assertFalse((candidate/'data/active-installation.json').exists())
