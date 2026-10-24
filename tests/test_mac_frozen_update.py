import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile
from build_mac_frozen_update import build, NAME
from mac_frozen_update import install, running_senders, unpack_verified_bridge


class FrozenMacTests(unittest.TestCase):
    def test_generated_command_is_single_executable_lf_file_with_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);bridge=root/'bridge.zip';bridge.write_bytes(b'fixture payload')
            target=root/'activation.zip';build(bridge,target)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(set(archive.namelist()),{NAME,'Прочитать.txt'})
                info=archive.getinfo(NAME)
                self.assertEqual((info.external_attr>>16)&0o777,0o755)
                command=archive.read(NAME)
                self.assertNotIn(b'\r\n',command)
                self.assertIn(hashlib.sha256(bridge.read_bytes()).hexdigest().encode(),command)
                python=command.split(b"<<'DVK_MAC_UPDATE_PYTHON'\n",1)[1].rsplit(b'DVK_MAC_UPDATE_PYTHON\n',1)[0]
                compile(python,'generated-command','exec')

    def test_corrupted_payload_rejected_before_import_or_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaises(ValueError):
                unpack_verified_bridge(b'wrong','0'*64,root)
            self.assertEqual(list(root.iterdir()),[])

    def test_running_old_sender_rejected_before_any_installation_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaisesRegex(RuntimeError,'закройте'):
                install(root,b'not-read','0'*64,running=lambda p:True)
            self.assertEqual(list(root.iterdir()),[])

    def test_process_guard_detects_relative_python_launch_in_original_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            def run(*args,**kwargs):
                if args[0][0]=='ps':
                    return SimpleNamespace(stdout='123 python3 main.py\n124 unrelated\n')
                return SimpleNamespace(stdout='p123\nfcwd\nn'+str(root)+'\n',returncode=0)
            with patch('mac_frozen_update.subprocess.run',side_effect=run), \
                 patch('mac_frozen_update.os.getuid',return_value=501,create=True), \
                 patch('mac_frozen_update.os.getpid',return_value=999):
                self.assertTrue(running_senders(root))
