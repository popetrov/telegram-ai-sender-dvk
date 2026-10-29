import json
import subprocess
import unittest
import urllib.error
from unittest.mock import MagicMock,patch
import launcher
import startup_update as update

class StableDiscoveryTests(unittest.TestCase):
    def test_platform_does_not_contact_rate_limited_api_when_stable_works(self):
        with patch.object(update,'stable_release_url',return_value=f'https://github.com/{update.REPOSITORY}/releases/tag/v2.0.35'),patch.object(update,'fetch') as fetch:
            release=update.release_metadata()
        self.assertEqual(release['tag_name'],'v2.0.35');fetch.assert_not_called()
        self.assertEqual(len(release['assets']),2)

    def test_foreign_redirect_is_rejected_without_api_or_feed(self):
        with patch.object(update,'stable_release_url',return_value='https://example.org/releases/tag/v2.0.99'),patch.object(update,'fetch') as fetch:
            with self.assertRaises(ValueError):update.release_metadata()
        fetch.assert_not_called()

    def test_windows_curl_keeps_certificate_validation(self):
        result=subprocess.CompletedProcess([],0,stdout=f'https://github.com/{update.REPOSITORY}/releases/tag/v2.0.35'.encode())
        with patch.object(update.urllib.request,'urlopen',side_effect=urllib.error.URLError('TLS')),patch.object(update.shutil,'which',return_value='curl.exe'),patch.object(update.subprocess,'run',return_value=result) as run:
            self.assertTrue(update.stable_release_url().endswith('v2.0.35'))
        args=run.call_args.args[0]
        self.assertNotIn('--insecure',args);self.assertNotIn('-k',args)
        self.assertIn('--proto-redir',args)

    def test_api_remains_fallback_when_stable_endpoint_is_unavailable(self):
        with patch.object(update,'stable_release_url',side_effect=urllib.error.URLError('offline')),patch.object(update,'fetch',return_value=json.dumps({'tag_name':'v2.0.35'}).encode()):
            self.assertEqual(update.release_metadata()['tag_name'],'v2.0.35')

    def test_legacy_launcher_avoids_api_when_stable_works(self):
        with patch.object(launcher,'get_latest_release_from_redirect',return_value={'tag':'v2.0.35'}),patch.object(launcher,'download_bytes') as fetch:
            self.assertEqual(launcher.get_latest_release()['tag'],'v2.0.35')
        fetch.assert_not_called()

    def test_legacy_never_chooses_pilot_from_feed(self):
        with patch.object(launcher,'get_latest_release_from_redirect',return_value=None),patch.object(launcher,'get_latest_release_from_atom') as feed:
            self.assertIsNone(launcher.get_latest_release_fallback())
        feed.assert_not_called()

if __name__=='__main__':unittest.main()
