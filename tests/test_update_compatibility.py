import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launcher


class ReleaseDiscoveryTests(unittest.TestCase):
    def test_extracts_release_page_url(self):
        self.assertEqual(
            launcher.extract_release_tag(
                "https://github.com/popetrov/telegram-ai-sender-dvk/releases/tag/v2.0.18"
            ),
            "v2.0.18",
        )

    def test_extracts_tag_from_atom_id(self):
        self.assertEqual(
            launcher.extract_release_tag(
                "tag:github.com,2008:Repository/123456/v2.0.18"
            ),
            "v2.0.18",
        )

    def test_rejects_unversioned_or_malformed_values(self):
        for value in (
            "https://github.com/popetrov/telegram-ai-sender-dvk/releases/latest",
            "v2.0",
            "v2.0.18-beta",
            "not-a-release",
        ):
            with self.subTest(value=value):
                self.assertEqual(launcher.extract_release_tag(value), "")

    def test_fallback_release_has_expected_asset_names(self):
        with patch("launcher.log"):
            release = launcher.release_info_for_tag("v2.0.19", "test")
        self.assertEqual(
            release["package_url"].rsplit("/", 1)[-1],
            "telegram_ai_sender_v2.0.19.zip",
        )
        self.assertEqual(
            release["checksum_url"].rsplit("/", 1)[-1],
            "telegram_ai_sender_v2.0.19.zip.sha256",
        )

    def test_update_allowlist_remains_compatible_with_v2_0_14(self):
        # Exact allowlist from the installed v2.0.14 launcher.
        legacy_allowlist = {
            "ai.py",
            "arbitration_api.py",
            "bot.py",
            "database.py",
            "launcher.py",
            "main.py",
            "products.json",
            "prompts/bg.md",
            "prompts/credit.md",
            "prompts/pravo_ip.md",
            "prompts/pravo_ooo.md",
            "prompts/zakupki_konsalt.md",
            "updater.py",
            "version.txt",
        }
        self.assertEqual(launcher.UPDATE_ALLOWLIST, legacy_allowlist)


if __name__ == "__main__":
    unittest.main()
