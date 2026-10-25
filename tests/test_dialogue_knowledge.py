import json
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock
from dialogue_knowledge import VectorKnowledge,KnowledgeUnavailable,source_quote_matches


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.text='Решение о выдаче банковской гарантии принимает банк.'
        row=Object(score=0.9,file_id='file-test',filename='knowledge.txt',content=[Object(type='text',text=self.text)])
        self.response=dict(answerable=True,answer=self.text,citations=[dict(evidence_id='1',quote=self.text)])
        self.client=Object(vector_stores=Object(search=AsyncMock(return_value=Object(data=[row]))),
                           responses=Object(create=AsyncMock(side_effect=lambda **kw:Object(status='completed',output_text=json.dumps(self.response)))))
        self.adapter=VectorKnowledge(self.client,business_id='dvk_finance',products=['БГ','Кредит'],vector_store_id='vs-test')
        self.query=dict(business_id='dvk_finance',product='БГ',question='Кто принимает решение?',request_id='req-test')

    async def test_retrieves_same_store_but_never_shares_chat_history_or_sends_telegram(self):
        result=await self.adapter.lookup(**self.query)
        self.assertEqual(result.answer,self.text)
        self.assertEqual(result.evidence[0].filename,'knowledge.txt')
        self.assertEqual(result.request_id,'req-test')
        call=self.client.responses.create.call_args.kwargs
        self.assertFalse(call['store'])
        self.assertNotIn('previous_response_id',call)
        self.assertEqual(self.client.vector_stores.search.call_args.kwargs['vector_store_id'],'vs-test')

    def test_source_quotes_ignore_only_whitespace_not_changed_conditions(self):
        self.assertTrue(source_quote_matches('Предоставьте ИНН и сумму гарантии','Предоставьте\nИНН\tи сумму\u00a0гарантии'))
        self.assertFalse(source_quote_matches('Комиссия составляет 1 процент','Комиссия составляет 5 процентов'))

    async def test_unknown_business_or_product_is_rejected_before_network(self):
        for patch in ({'business_id':'other'},{'product':'Николай'}):
            with self.assertRaises(PermissionError):await self.adapter.lookup(**{**self.query,**patch})
        self.client.vector_stores.search.assert_not_awaited()

    async def test_no_evidence_stops_without_ai_answer(self):
        self.client.vector_stores.search.return_value=Object(data=[])
        with self.assertRaises(KnowledgeUnavailable):await self.adapter.lookup(**self.query)
        self.client.responses.create.assert_not_awaited()

    async def test_invented_source_or_quote_is_rejected(self):
        for citations in ([dict(evidence_id='99',quote=self.text)],[dict(evidence_id='1',quote='Guaranteed bank approval')],[]):
            self.response['citations']=citations
            with self.assertRaises(KnowledgeUnavailable):await self.adapter.lookup(**self.query)

    async def test_missing_answer_and_partial_output_do_not_become_client_messages(self):
        self.response['answerable']=False
        with self.assertRaises(KnowledgeUnavailable):await self.adapter.lookup(**self.query)
        self.client.responses.create.side_effect=None
        self.client.responses.create.return_value=Object(status='incomplete',output_text='partial')
        with self.assertRaises(KnowledgeUnavailable):await self.adapter.lookup(**self.query)

    async def test_provider_error_does_not_expose_secret_details(self):
        self.client.vector_stores.search.side_effect=RuntimeError('secret-api-key')
        with self.assertRaises(KnowledgeUnavailable) as caught:
            await self.adapter.lookup(**self.query)
        self.assertNotIn('secret-api-key',str(caught.exception))
