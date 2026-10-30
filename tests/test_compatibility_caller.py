import contextlib,json,os,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from unified_install import compatibility_caller

def install(p,key=b'x'*256,manager='manager_test'):
 p.mkdir();(p/'data').mkdir();(p/'.env').write_text('MANAGER_ID='+manager+'\nSESSION_NAME=session\n')
 with contextlib.closing(sqlite3.connect(p/'session.session')) as db,db:
  db.execute('CREATE TABLE sessions(auth_key BLOB)');db.execute('INSERT INTO sessions VALUES(?)',(key,))
 return p.resolve()
class CallerTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.base=Path(self.t.name).resolve()
  self.root=install(self.base/'DVK Finance программа')
  self.source=install(self.base/(self.root.name+'_dvk_'+uuid.uuid4().hex))
  (self.root/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=self.source.name)))
  self.target=self.base/(self.root.name+'_dvk_'+uuid.uuid4().hex)
 def test_frozen_launcher_without_environment_recovers_real_original(self):
  with patch.dict(os.environ,{},clear=True):self.assertEqual(compatibility_caller(self.source,self.target),self.root)
  self.assertFalse(self.target.exists())
 def test_stale_environment_does_not_override_verified_pointer(self):
  with patch.dict(os.environ,{'DVK_LAUNCHER_ROOT':str(self.base/'stale')}):self.assertEqual(compatibility_caller(self.source,self.target),self.root)
 def test_flat_launcher_remains_supported(self):
  self.assertEqual(compatibility_caller(self.root,self.target),self.root)
 def test_unrelated_same_account_folder_cannot_claim_source(self):
  other=install(self.base/'unrelated');candidate=self.base/(other.name+'_dvk_'+uuid.uuid4().hex)
  with self.assertRaises(ValueError):compatibility_caller(self.source,candidate)
 def test_foreign_key_cannot_become_launcher(self):
  other=install(self.base/'other-account',key=b'y'*256)
  (other/'data/active-installation.json').write_text(json.dumps(dict(format=1,directory=self.source.name)))
  with self.assertRaises(ValueError):compatibility_caller(self.source,self.base/(other.name+'_dvk_'+uuid.uuid4().hex))
 def test_invalid_destination_and_broken_pointer_never_create_anything(self):
  for candidate in [self.base/'arbitrary',self.base/('missing_dvk_'+uuid.uuid4().hex)]:
   with self.assertRaises(ValueError):compatibility_caller(self.source,candidate)
   self.assertFalse(candidate.exists())
 def test_windows_short_and_mixed_case_launcher_names(self):
  if os.name!='nt':self.skipTest('Windows paths')
  import ctypes
  output=ctypes.create_unicode_buffer(32768)
  count=ctypes.windll.kernel32.GetShortPathNameW(str(self.root),output,len(output))
  self.assertGreater(count,0)
  for stem in [Path(output.value).name,self.root.name.upper()]:
   candidate=self.base/(stem+'_dvk_'+uuid.uuid4().hex)
   self.assertEqual(compatibility_caller(self.source,candidate),self.root)
