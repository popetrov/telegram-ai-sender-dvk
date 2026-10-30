import contextlib,hashlib,json,os,sqlite3,subprocess,sys,tempfile,unittest,uuid,zipfile
from pathlib import Path
from unittest.mock import patch
import unified_install as u
from test_unified_investigation import fixture,payload,C

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.base=Path(self.t.name).resolve();self.root=fixture(self.base/'launcher')
 def shell(self):
  stub=self.base/(self.root.name+'_dvk_'+uuid.uuid4().hex)
  u.prepare_legacy(self.root,stub,payload(),check=lambda *a:None)
  return stub
 def test_windows_own_cwd_reproduces_error32_and_cleanup_after_chdir(self):
  if os.name!='nt':self.skipTest('Windows only')
  folder=self.base/'empty';folder.mkdir();old=Path.cwd()
  try:
   os.chdir(folder)
   with self.assertRaises(PermissionError):folder.rmdir()
  finally:os.chdir(old)
  folder.rmdir()
 def test_busy_shell_is_nonfatal_and_retried_after_holder_exit(self):
  stub=self.shell()
  holder=subprocess.Popen([sys.executable,'-c','import sys;print("ready",flush=True);sys.stdin.read()'],cwd=stub,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
  self.assertEqual(holder.stdout.readline().strip(),'ready')
  try:
   result=u.clean_transition_stubs(self.root)
   if os.name=='nt':self.assertGreaterEqual(result['deferred'],1)
  finally:holder.stdin.close();holder.wait(timeout=10);holder.stdout.close()
  u.clean_transition_stubs(self.root)
  self.assertFalse(stub.exists());self.assertFalse(list((self.root/'.dvk/transition-cleanup').glob('*.json')))
 def test_windows_short_name_shell_cleanup(self):
  if os.name!='nt':self.skipTest('Windows only')
  import ctypes
  renamed=self.base/'DVK Finance программа';self.root.rename(renamed);self.root=renamed
  output=ctypes.create_unicode_buffer(32768)
  self.assertGreater(ctypes.windll.kernel32.GetShortPathNameW(str(self.root),output,len(output)),0)
  stub=self.base/(Path(output.value).name+'_dvk_'+uuid.uuid4().hex)
  u.prepare_legacy(self.root,stub,payload(),check=lambda *a:None)
  u.clean_transition_stubs(self.root)
  self.assertFalse(stub.exists())
 def test_unknown_files_never_removed(self):
  stub=self.shell();(stub/'my-client-list.xlsx').write_bytes(b'keep')
  u.clean_transition_stubs(self.root)
  self.assertEqual((stub/'my-client-list.xlsx').read_bytes(),b'keep');self.assertTrue((stub/'main.py').exists())
 def test_legacy_empty_shell_removed_but_data_folder_preserved(self):
  empty=self.base/(self.root.name+'_dvk_'+uuid.uuid4().hex);empty.mkdir()
  personal=self.base/(self.root.name+'_dvk_'+uuid.uuid4().hex);personal.mkdir();(personal/'.env').write_text('keep')
  u.clean_transition_stubs(self.root)
  self.assertFalse(empty.exists());self.assertTrue((personal/'.env').exists())
 def test_activation_keeps_batch_byte_for_byte_and_repairs_saved_python(self):
  batch=b'@echo off\r\necho Original startup script\r\n'
  (self.root/'start.bat').write_bytes(batch)
  (self.root/'data/python-path.txt').write_text('obsolete/python.exe')
  u.activate(self.root,u.prepare_code(self.root,payload()),self.root,check=lambda *a:None)
  self.assertEqual((self.root/'start.bat').read_bytes(),batch)
  expected='venv\\Scripts\\python.exe' if os.name=='nt' else './venv/bin/python'
  self.assertEqual((self.root/'data/python-path.txt').read_text().strip(),expected)
 def test_real_cmd_does_not_reenter_rewritten_script(self):
  if os.name!='nt':self.skipTest('Windows only')
  batch=(C/'start.bat').read_bytes();(self.root/'start.bat').write_bytes(batch)
  script='''from pathlib import Path
import sys
sys.path.insert(0,sys.argv[1] if len(sys.argv)>1 else REPLACE)
import unified_install as u
from manual_update import package_files
code=Path(u.__file__).parent;root=Path(__file__).parent
p={n:(code/n).read_bytes() for n in package_files(code)}
u.activate(root,u.prepare_code(root,p),root,check=lambda *a:None)
print('STEP_COMPLETED')
'''.replace('REPLACE',repr(str(C)))
  (self.root/'start.py').write_text(script,encoding='utf-8')
  r=subprocess.run(['cmd.exe','/d','/c',str(self.root/'start.bat')],cwd=self.root,capture_output=True,timeout=30)
  self.assertEqual(r.returncode,0,r.stderr.decode(errors='replace'))
  self.assertEqual(r.stdout.count(b'STEP_COMPLETED'),1,r.stdout)
  self.assertEqual(r.stderr,b'');self.assertEqual((self.root/'start.bat').read_bytes(),batch)
