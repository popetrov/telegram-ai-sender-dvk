import json
from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock
from dialogue_ai import DialogueAI
from dialogue_knowledge import KnowledgeUnavailable,Evidence,KnowledgeAnswer


class DialogueAITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client=Object(responses=Object(create=AsyncMock(return_value=Object(status='completed',output_text='Summary'))))
        self.ai=DialogueAI(self.client,context_chars=4000)
        self.history=[dict(telegram_id=i,direction='incoming' if i%2 else 'outgoing',text=f'message-{i} '+('x'*700),sent_at=100+i) for i in range(25)]

    async def test_long_history_processes_every_message_without_shared_response_state(self):
        context=await self.ai.context(self.history)
        covered=[]
        for call in self.client.responses.create.call_args_list:
            values=call.kwargs
            self.assertFalse(values['store'])
            self.assertNotIn('previous_response_id',values)
            covered.extend(m['id'] for m in json.loads(values['input'])['messages'])
        covered.extend(m['id'] for m in context['history'])
        self.assertEqual(covered,list(range(25)))
        self.assertEqual(context['processed_messages'],25)

    async def test_plain_stop_request_wins_without_ai_call(self):
        history=[dict(telegram_id=1,direction='incoming',text='Не пишите мне больше',sent_at=100)]
        result=await self.ai.analyze(thread=dict(product='БГ'),history=history,now=1000)
        self.assertEqual(result['intent'],'opt_out')
        self.client.responses.create.assert_not_awaited()

    async def test_malformed_decision_or_model_failure_requires_manager(self):
        self.client.responses.create.return_value=Object(status='completed',output_text='not-json')
        with self.assertRaises(KnowledgeUnavailable):
            await self.ai.analyze(thread=dict(product='БГ'),history=[],now=1000)
        self.client.responses.create.side_effect=TimeoutError('private detail')
        with self.assertRaises(KnowledgeUnavailable) as caught:
            await self.ai.analyze(thread=dict(product='БГ'),history=[],now=1000)
        self.assertNotIn('private detail',str(caught.exception))

    async def test_writer_uses_sources_and_complete_client_context(self):
        knowledge=KnowledgeAnswer('Confirmed fact',(Evidence('1','file','source.txt','Confirmed fact'),),'request')
        self.client.responses.create.return_value=Object(status='completed',output_text='Ответ клиенту')
        result=await self.ai.compose(thread=dict(product='БГ'),history=self.history[:2],decision=None,knowledge=knowledge,stage=1)
        self.assertEqual(result,'Ответ клиенту')
        payload=json.loads(self.client.responses.create.call_args.kwargs['input'])
        self.assertEqual(len(payload['context']['history']),2)
        self.assertEqual(payload['evidence'][0]['file'],'source.txt')
