import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import manual_update
import startup_update
from build_platform_release import ROOT, build_transition


class BundleIsolationTests(unittest.TestCase):
    def test_installed_old_allowlist_cannot_reject_new_bundle_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);package=folder/'manager.zip'
            manual_update.build_package(ROOT,package)
            raw=folder/'bundle.zip';build_transition(ROOT,raw,package)
            bundle=folder/'bundle';bundle.mkdir()
            startup_update.unpack_bundle(raw.read_bytes(),bundle)
            # Model the released 2.0.23 bundle, which has prepare_update only.
            # It must import its own validator rather than the installed one.
            code=('from pathlib import Path\nimport manual_update,json\n'
                  'def prepare_update(source,candidate,bundle,tag):\n'
                  ' manifest=json.loads((bundle/"transition-manifest.json").read_text("utf-8"))\n'
                  ' manual_update.verify_package(bundle/"manager-package.zip",manifest["files"]["manager-package.zip"])\n'
                  ' candidate.mkdir()\n (candidate/"verified.txt").write_text(manual_update.__file__,encoding="utf-8")\n')
            (bundle/'startup_update.py').write_text(code,encoding='utf-8')
            manifest_path=bundle/'transition-manifest.json'
            manifest=json.loads(manifest_path.read_text('utf-8'))
            manifest['files']['startup_update.py']=hashlib.sha256((bundle/'startup_update.py').read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
            candidate=folder/'candidate'
            with patch.object(manual_update,'verify_package',side_effect=ValueError('Forbidden archive entry: DIALOGUES.md')):
                startup_update.prepare_update(folder,candidate,bundle,'v2.0.23')
            self.assertEqual(Path((candidate/'verified.txt').read_text('utf-8')).resolve(),(bundle/'manual_update.py').resolve())

