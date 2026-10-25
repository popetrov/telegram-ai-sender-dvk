import hashlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
import startup_update as update


class Page:
    def __init__(self,url):self.url=url
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def geturl(self):return self.url


class FallbackTests(unittest.TestCase):
    def fetch(self,url):
        if url==update.API:raise urllib.error.HTTPError(url,403,'rate limit',{},None)
        if url.endswith('.sha256'):return self.checksum
        return self.package

    def setUp(self):
        self.package=b'official-package'
        self.checksum=(hashlib.sha256(self.package).hexdigest()+'  dvk-transition-pilot.zip').encode()
        self.url=f'https://github.com/{update.REPOSITORY}/releases/tag/v2.0.22'

    def test_rate_limited_api_uses_official_stable_page_and_verifies_checksum(self):
        with patch.object(update,'fetch',side_effect=self.fetch),patch.object(update.urllib.request,'urlopen',return_value=Page(self.url)):
            self.assertEqual(update.latest_bundle('2.0.20'),('v2.0.22',self.package))

    def test_fallback_still_rejects_corrupt_package(self):
        self.checksum=b'0'*64
        with patch.object(update,'fetch',side_effect=self.fetch),patch.object(update.urllib.request,'urlopen',return_value=Page(self.url)):
            with self.assertRaises(ValueError):update.latest_bundle('2.0.20')

    def test_login_external_and_unversioned_redirects_are_rejected(self):
        for url in ['https://github.com/login','https://example.com/releases/tag/v2.0.22',self.url+'?unexpected=1',self.url.replace('v2.0.22','pilot-test')]:
            with patch.object(update,'fetch',side_effect=self.fetch),patch.object(update.urllib.request,'urlopen',return_value=Page(url)):
                with self.assertRaises(ValueError):update.latest_bundle('2.0.20')

    def test_working_api_prerelease_is_not_replaced_by_fallback(self):
        with patch.object(update,'fetch',return_value=json.dumps(dict(prerelease=True)).encode()),patch.object(update.urllib.request,'urlopen') as network:
            self.assertIsNone(update.latest_bundle('2.0.20'));network.assert_not_called()

    def test_same_stable_version_does_not_download_a_package(self):
        with patch.object(update,'fetch',side_effect=self.fetch) as fetch,patch.object(update.urllib.request,'urlopen',return_value=Page(self.url)):
            self.assertIsNone(update.latest_bundle('2.0.22'))
            self.assertEqual(fetch.call_count,1)


if __name__=='__main__':unittest.main()
