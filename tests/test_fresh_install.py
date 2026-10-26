import hashlib,json,os,tempfile,threading,unittest,urllib.request,urllib.error,urllib.parse,zipfile
from pathlib import Path
from unittest.mock import patch
from dotenv import dotenv_values
from build_fresh_installer import build,COMMON,PLATFORM,GUIDE
from manual_update import build_package,verify_package
import setup_wizard as setup

ROOT=Path(__file__).resolve().parents[1]

class FreshInstallTests(unittest.TestCase):
    def values(self):return dict(number='010',TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='a'*32,
        BOT_TOKEN='12345:'+'x'*30,OPENAI_API_KEY='test-key',PARSER_API_KEY='')

    def test_preserves_leading_zero_identity_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'center-public.json').write_bytes((ROOT/'center-public.json').read_bytes())
            setup.save(root,self.values());original=(root/'.env').read_bytes()
            values=dotenv_values(root/'.env')
            self.assertEqual(values['MANAGER_ID'],'manager_010');self.assertEqual(values['SESSION_NAME'],'telegram_sender_manager_010')
            self.assertTrue(values['ACCESS_PUBLIC_KEY']);self.assertTrue(values['SYNC_PUBLIC_KEY'])
            with self.assertRaises(ValueError):setup.save(root,self.values())
            self.assertEqual(original,(root/'.env').read_bytes())

    def test_installer_contains_only_runtime_and_setup_not_personal_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);package=folder/'manager.zip';build_package(ROOT,package)
            _,runtime=verify_package(package,hashlib.sha256(package.read_bytes()).hexdigest())
            for platform in PLATFORM:
                output=folder/(platform+'.zip');build(ROOT,package,output,platform)
                with zipfile.ZipFile(output) as archive:
                    self.assertEqual(set(archive.namelist()),set(runtime)|COMMON|PLATFORM[platform]|{'data/installed-baseline.json',GUIDE})
                    self.assertNotIn('.env',archive.namelist())
                    baseline=json.loads(archive.read('data/installed-baseline.json'))
                    for name,data in runtime.items():self.assertEqual(baseline[name],data.decode('utf-8-sig'))
                    if platform=='windows':self.assertTrue(archive.read('install_windows.ps1').startswith(b'\xef\xbb\xbf'))
                    for name in PLATFORM[platform]:
                        if name.endswith(('.sh','.command')):self.assertTrue(archive.getinfo(name).external_attr>>16 & 0o111)

    def test_local_form_rejects_foreign_origin_and_never_saves_demo(self):
        ready=threading.Event();address=[]
        def callback(url):address.append(url);ready.set()
        with tempfile.TemporaryDirectory() as temporary:
            thread=threading.Thread(target=setup.wizard,args=(Path(temporary),),kwargs=dict(demo=True,open_browser=False,ready=callback),daemon=True)
            thread.start();self.assertTrue(ready.wait(5));url=address[0];token=url.rsplit('/',1)[1]
            with urllib.request.urlopen(url) as response:
                body=response.read().decode();self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn('type="password"',body)
            data=urllib.parse.urlencode(dict(self.values(),csrf=token)).encode()
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(urllib.request.Request(url,data=data,headers={'Origin':'https://example.com'}))
            self.assertEqual(rejected.exception.code,403)
            origin=url.split('/setup/')[0]
            with urllib.request.urlopen(urllib.request.Request(url,data=data,headers={'Origin':origin})) as response:self.assertEqual(response.status,200)
            thread.join(5);self.assertFalse(thread.is_alive());self.assertFalse((Path(temporary)/'.env').exists())

    def test_token_validation_does_not_include_pasted_secret_in_error(self):
        values=self.values();values['BOT_TOKEN']='secret pasted incorrectly'
        with self.assertRaises(ValueError) as error:setup.validate(values)
        self.assertNotIn('secret pasted incorrectly',str(error.exception))
