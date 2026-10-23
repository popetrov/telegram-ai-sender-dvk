from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manual_update as updater


class ManualUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root/"release"
        self.release.mkdir()
        for name in updater.RUNTIME:
            (self.release/name).write_text("VALUE = 1\n" if name.endswith(".py") else "content\n", encoding="utf-8")
        (self.release/"version.txt").write_text("2.0.20\n", encoding="utf-8")
        self.products = {"БГ": {"prompt": "bg.md", "aliases": ["бг"], "context_adapters": []}}
        (self.release/"products.json").write_text(json.dumps(self.products), encoding="utf-8")
        (self.release/"prompts").mkdir()
        (self.release/"prompts/bg.md").write_text("incoming prompt", encoding="utf-8")
        self.zip = self.root/"release.zip"
        self.digest = updater.build_package(self.release, self.zip)
        self.source = self.root/"working"
        self.source.mkdir()
        (self.source/"version.txt").write_text("2.0.9\n", encoding="utf-8")
        (self.source/".env").write_text("LOCAL_SECRET=keep-locally\n", encoding="utf-8")
        self.destination = self.root/"candidate"

    def test_prepares_without_changing_working_files_or_secrets_in_package(self):
        (self.source/"prompts").mkdir()
        (self.source/"prompts/bg.md").write_text("my prompt", encoding="utf-8")
        (self.source/"prompts/khimikaty.md").write_text("paused draft", encoding="utf-8")
        local_products = {"БГ": {"prompt": "bg.md", "aliases": ["custom"]},
                          "Николай": {"prompt": "Nikolai.md"}}
        (self.source/"products.json").write_text(json.dumps(local_products), encoding="utf-8")
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        report = updater.prepare(self.source, self.zip, self.digest, self.destination)
        after = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        merged = json.loads((self.destination/"products.json").read_text(encoding="utf-8"))
        self.assertIn("Николай", merged)
        self.assertEqual(merged["БГ"]["aliases"], ["custom"])
        self.assertEqual(merged["БГ"]["context_adapters"], [])
        self.assertEqual((self.destination/"prompts/bg.md").read_text(encoding="utf-8"), "my prompt")
        self.assertEqual((self.destination/"prompts/khimikaty.md").read_text(encoding="utf-8"), "paused draft")
        self.assertEqual((self.destination/".env").read_bytes(), before[".env"])
        self.assertFalse(report["ready_to_run"])
        self.assertIn("prompts/bg.md", report["conflicts"])
        with zipfile.ZipFile(self.zip) as archive:
            self.assertNotIn(".env", archive.namelist())
            self.assertFalse(any(name.startswith("data/") for name in archive.namelist()))

    def test_bad_checksum_and_malicious_archive_leave_no_candidate(self):
        with self.assertRaises(ValueError):
            updater.prepare(self.source, self.zip, "0"*64, self.destination)
        self.assertFalse(self.destination.exists())
        with zipfile.ZipFile(self.zip, "a") as archive:
            archive.writestr("../escape.py", "bad")
        digest = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            updater.prepare(self.source, self.zip, digest, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root/"escape.py").exists())

    def test_preserved_python_syntax_failure_cleans_up_candidate(self):
        (self.source/"main.py").write_text("broken syntax !", encoding="utf-8")
        with patch("manual_update.git_baseline", return_value=b"VALUE = 0\n"):
            with self.assertRaises(SyntaxError):
                updater.prepare(self.source, self.zip, self.digest, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source/"main.py").read_text(encoding="utf-8"), "broken syntax !")

    def test_sqlite_snapshot_includes_committed_wal_and_excludes_authority_keys(self):
        data = self.source/"data"
        data.mkdir()
        connection = sqlite3.connect(data/"local.sqlite")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE clients (name TEXT)")
        connection.execute("INSERT INTO clients VALUES ('client')")
        connection.commit()
        (data/"access_authority").mkdir()
        (data/"access_authority/signing.key").write_bytes(b"private-key")
        updater.prepare(self.source, self.zip, self.digest, self.destination)
        with closing(sqlite3.connect(self.destination/"data/local.sqlite")) as snapshot:
            self.assertEqual(snapshot.execute("SELECT name FROM clients").fetchall(), [("client",)])
        self.assertFalse((self.destination/"data/access_authority").exists())

    def test_snapshot_closes_both_connections_before_return(self):
        source = self.source/"source.sqlite"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE clients (name TEXT)")
            connection.commit()
        opened = []
        original = sqlite3.connect
        class TrackingConnection(sqlite3.Connection):
            closed = False
            def close(self):
                super().close()
                self.closed = True
        def connect(*args, **kwargs):
            connection = original(*args, factory=TrackingConnection, **kwargs)
            opened.append(connection)
            return connection
        with patch("manual_update.sqlite3.connect", side_effect=connect):
            updater.snapshot_file(source, self.root/"copy.sqlite")
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(connection.closed for connection in opened))

    def test_three_way_merge_preserves_changes_and_reports_real_conflicts(self):
        base = {"product": {"prompt": "p.md", "model": "old", "aliases": ["a"]}}
        local = {"product": {"prompt": "p.md", "model": "local", "aliases": ["a"]}}
        incoming = {"product": {"prompt": "p.md", "model": "remote", "aliases": ["a", "b"]}}
        conflicts = []
        result = updater.merge_json(base, local, incoming, conflicts)
        self.assertEqual(result["product"]["model"], "local")
        self.assertEqual(result["product"]["aliases"], ["a", "b"])
        self.assertEqual(conflicts, ["products.product.model"])

    def test_wrapped_registry_keeps_local_products(self):
        (self.source/"products.json").write_text(json.dumps({"Custom": {"prompt": "custom.md"}}), encoding="utf-8")
        (self.release/"products.json").write_text(json.dumps({"schema_version": 1, "products": self.products}), encoding="utf-8")
        other_zip = self.root/"wrapped.zip"
        digest = updater.build_package(self.release, other_zip)
        updater.prepare(self.source, other_zip, digest, self.destination)
        result = json.loads((self.destination/"products.json").read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], 1)
        self.assertIn("Custom", result["products"])
        self.assertIn("БГ", result["products"])

    def test_existing_and_nested_destinations_rejected(self):
        for destination in [self.source, self.source/"child", self.root]:
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                updater.prepare(self.source, self.zip, self.digest, destination)

    def test_symlink_in_local_data_rejected_without_changing_source(self):
        (self.source/"data").mkdir()
        (self.source/"data/link").symlink_to(self.release, target_is_directory=True)
        with self.assertRaises(ValueError):
            updater.prepare(self.source, self.zip, self.digest, self.destination)
        self.assertFalse(self.destination.exists())

    def test_downgrade_is_rejected_before_copying_data(self):
        (self.source/"version.txt").write_text("2.0.21", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "newer version"):
            updater.prepare(self.source, self.zip, self.digest, self.destination)
        self.assertFalse(self.destination.exists())

    def test_windows_reserved_names_rejected(self):
        for name in ("prompts/CON.md", "prompts/aux.md", "prompts/LPT1.md", "prompts/file.md."):
            with self.subTest(name=name):
                self.assertFalse(updater.allowed_name(name))

    def test_case_collision_rejected(self):
        with zipfile.ZipFile(self.zip, "a") as archive:
            archive.writestr("MAIN.py", "VALUE=2")
        digest = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            updater.verify_package(self.zip, digest)


if __name__ == "__main__":
    unittest.main()
