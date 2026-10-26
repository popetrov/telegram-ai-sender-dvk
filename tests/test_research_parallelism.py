import asyncio,os,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
for name,value in dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='test',OPENAI_API_KEY='test',BOT_TOKEN='123:test',MANAGER_ID='test').items():
    os.environ.setdefault(name,value)
import bot

class ResearchPolicyTests(unittest.TestCase):
    def test_only_connected_owner_account_gets_ten(self):
        for account,expected in [(925600480,10),(7401283669,1),(42,1),(None,1),('925600480',1)]:
            with self.subTest(account=account),patch('access_runtime.account_id',account):
                self.assertEqual(bot.parallel_research_limit(),expected)

class ResearchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def run_preparation(self,account,expected):
        active=peak=0
        async def prepare(row):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            try:
                await asyncio.sleep(.01)
                return 'prepared','saved',dict(prepared_message_id=row['index'],final_message='saved')
            finally:active-=1
        reply=AsyncMock();update=SimpleNamespace(effective_user=SimpleNamespace(id=925600480),
            effective_chat=SimpleNamespace(id=925600480),effective_message=SimpleNamespace(reply_text=reply))
        app=SimpleNamespace(bot_data={})
        rows=[dict(index=i,inn=str(i),username='test_'+str(i)) for i in range(12)]
        context=SimpleNamespace(application=app,user_data=dict(pending_rows=rows,pending_file_name='synthetic.xlsx'))
        with patch('access_runtime.account_id',account),patch('bot.user_is_allowed',return_value=True), \
             patch('bot.save_import'),patch('bot.prepare_one_client',side_effect=prepare):
            await bot.confirm_command(update,context)
        self.assertEqual(peak,expected)
        self.assertIn('Одновременных исследований: '+str(expected)+'.',reply.call_args_list[0].args[0])
        queue=app.bot_data[bot.QUEUE_KEY]
        self.assertEqual(queue['state'],'awaiting_time')
        self.assertEqual(sorted(x['prepared_message_id'] for x in queue['items']),list(range(12)))
        self.assertEqual(queue['preparation_counts']['prepared'],12)
        self.assertNotIn(bot.PREPARING_KEY,app.bot_data)
    async def test_manager_is_serial_even_when_chat_id_matches_owner(self):
        await self.run_preparation(7401283669,1)
    async def test_owner_retains_ten_simultaneous_research_tasks(self):
        await self.run_preparation(925600480,10)
