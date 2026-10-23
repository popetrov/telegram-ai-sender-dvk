import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "requirements.txt").write_text("example==1\n", encoding="utf-8")
        self.python = start.environment_python(self.root)
        self.python.parent.mkdir(parents=True)
        self.python.touch()

    def test_unchanged_dependencies_are_not_reinstalled(self):
        digest = hashlib.sha256((self.root/"requirements.txt").read_bytes()).hexdigest()
        (self.root/"venv/.requirements.sha256").write_text(digest, encoding="utf-8")
        with patch("start.subprocess.run") as run:
            self.assertEqual(start.prepare_environment(self.root), self.python)
        self.assertEqual(run.call_count, 1)  # Interpreter compatibility only.

    def test_failed_install_never_marks_environment_ready(self):
        def run(command, **kwargs):
            if "install" in command:
                raise subprocess.CalledProcessError(1, command)
        with patch("start.subprocess.run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                start.prepare_environment(self.root)
        self.assertFalse((self.root/"venv/.requirements.sha256").exists())

    def test_successful_install_records_exact_requirements(self):
        with patch("start.subprocess.run") as run:
            start.prepare_environment(self.root)
        self.assertEqual(run.call_count, 3)  # Version, install, dependency check.
        self.assertEqual((self.root/"venv/.requirements.sha256").read_text(encoding="utf-8").strip(),
                         hashlib.sha256((self.root/"requirements.txt").read_bytes()).hexdigest())

    def test_application_exit_status_is_preserved(self):
        with patch("start.subprocess.run", return_value=Mock(returncode=7)):
            self.assertEqual(start.command_status(["fake"], root=self.root), 7)

    def test_existing_incomplete_environment_is_not_deleted(self):
        self.python.unlink()
        marker = self.root/"venv/keep.txt"
        marker.write_text("user file", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            start.prepare_environment(self.root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user file")
