from pathlib import Path
import tempfile
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from authority_store import AuthorityStore
from sync_protocol import public_text
from dialogue_knowledge import Evidence,KnowledgeAnswer,KnowledgeUnavailable
from dialogue_knowledge_protocol import make_request,read_response,KnowledgeBroker


class KnowledgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.authority=AuthorityStore(Path(self.temp.name)/'center.sqlite')
        self.authority.register(42,'manager_001:42');self.authority.assign(42,'dvk')
        self.signing=Ed25519PrivateKey.generate();self.encryption=X25519PrivateKey.generate()
        self.provider=Object(products={'БГ'},lookup=AsyncMock(return_value=KnowledgeAnswer('Confirmed fact',(Evidence('1','file','source','Confirmed fact'),),'req')))
        self.broker=KnowledgeBroker(self.authority,{('dvk','dvk_finance'):self.provider},self.signing,self.encryption,clock=lambda:1000)

    def request(self,**patch):
        values=dict(subject='manager_001:42',business_id='dvk_finance',product='БГ',question='A question')
        return make_request(public_text(self.encryption),clock=lambda:1000,**{**values,**patch})

    def result(self,packet,request,private):
        return read_response(packet,request=request,private_key=private,signing_public_key=self.signing.public_key(),clock=lambda:1000)

    async def test_result_is_encrypted_and_signed_for_exact_request_and_account(self):
        request,private,packet=self.request()
        response=await self.broker.handle(packet,account_id=42)
        result=self.result(response,request,private)
        self.assertEqual(result.answer,'Confirmed fact')
        self.assertEqual(result.request_id,request['request_id'])
        with self.assertRaises(Exception):self.result(response,request,X25519PrivateKey.generate())
        with self.assertRaises(ValueError):self.result(response,{**request,'question':'another question'},private)

    async def test_forged_account_or_wrong_company_never_queries_private_knowledge(self):
        request,private,packet=self.request()
        with self.assertRaises(PermissionError):await self.broker.handle(packet,account_id=43)
        self.authority.company('other');self.authority.register(43,'manager_002:43');self.authority.assign(43,'other')
        request,private,packet=self.request(subject='manager_002:43')
        response=await self.broker.handle(packet,account_id=43)
        with self.assertRaises(KnowledgeUnavailable):self.result(response,request,private)
        self.provider.lookup.assert_not_awaited()

    async def test_revoked_access_during_query_does_not_release_knowledge(self):
        async def lookup(**kw):
            self.authority.revoke(42)
            return KnowledgeAnswer('Private result',(Evidence('1','file','source','Private result'),),'req')
        self.provider.lookup.side_effect=lookup
        request,private,packet=self.request()
        response=await self.broker.handle(packet,account_id=42)
        with self.assertRaises(KnowledgeUnavailable):self.result(response,request,private)

    async def test_unknown_product_or_business_cannot_change_owner_provider(self):
        for values in ({'product':'Николай'},{'business_id':'another_business'}):
            request,private,packet=self.request(**values)
            response=await self.broker.handle(packet,account_id=42)
            with self.assertRaises(KnowledgeUnavailable):self.result(response,request,private)
        self.provider.lookup.assert_not_awaited()

    async def test_expired_request_is_rejected_before_provider(self):
        self.broker.clock=lambda:1700
        _,_,packet=self.request()
        with self.assertRaises(ValueError):await self.broker.handle(packet,account_id=42)
        self.provider.lookup.assert_not_awaited()
