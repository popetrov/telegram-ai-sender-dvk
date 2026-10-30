import asyncio,base64,json,unittest,os
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from access_lease import canonical
import monitor_capabilities as caps

class CapabilityTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  caps._cached.clear();self.key=Ed25519PrivateKey.generate();self.client=Mock()
  self.client.get_entity=AsyncMock(return_value=SimpleNamespace(id=7,bot=True,username='service_bot'))
  self.client.get_me=AsyncMock(return_value=SimpleNamespace(id=42))
  self.conversation=AsyncMock();self.conversation.__aenter__.return_value=self.conversation
  self.client.conversation.return_value=self.conversation
  self.settings={'ACCESS_BOT_USERNAME':'service_bot','ACCESS_PUBLIC_KEY':base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}
  async def send(text,**kw):
   self.nonce=text.split()[1];return SimpleNamespace(id=99)
  self.conversation.send_message.side_effect=send
 def response(self,account=42,key=None):
  payload=dict(protocol='DVK_MONITOR_CAPS_V1',account_id=account,nonce=self.nonce,forensic_v1=True)
  envelope=dict(payload=payload,signature=base64.b64encode((key or self.key).sign(canonical(payload))).decode())
  return SimpleNamespace(raw_text='DVK_MONITOR_CAPS '+json.dumps(envelope))
 async def test_correct_signature_and_account_allow_forensic_upload(self):
  async def response(*args):return self.response()
  self.conversation.get_response.side_effect=response
  with patch.dict(os.environ,self.settings):self.assertTrue(await caps.supported(self.client))
 async def test_foreign_account_and_wrong_signature_do_not_enable_protocol(self):
  for account,key in [(43,None),(42,Ed25519PrivateKey.generate())]:
   caps._cached.clear()
   async def response(*args):return self.response(account,key)
   self.conversation.get_response.side_effect=response
   with patch.dict(os.environ,self.settings):self.assertFalse(await caps.supported(self.client))
 async def test_old_center_timeout_only_defers_telemetry(self):
  self.conversation.get_response.side_effect=TimeoutError()
  with patch.dict(os.environ,self.settings):self.assertFalse(await caps.supported(self.client))
  self.client.disconnect.assert_not_called()
