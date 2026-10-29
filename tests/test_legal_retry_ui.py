import os,unittest
from types import SimpleNamespace as Object
from unittest.mock import AsyncMock,Mock,patch
os.environ.setdefault('TELEGRAM_API_ID','12345')
os.environ.setdefault('TELEGRAM_API_HASH','fixture')
import bot


class RetryUITests(unittest.IsolatedAsyncioTestCase):
    def setup_context(self,state='awaiting_time'):
        self.old_item=dict(prepared_message_id=1,inn='1234567890',username='ready',product='Право_ООО')
        self.old=dict(state=state,items=[self.old_item],send_at=None)
        self.ctx=Object(application=Object(bot_data={bot.QUEUE_KEY:self.old}),user_data={})
        self.update=Object(effective_user=Object(id=42),effective_chat=Object(id=42),effective_message=Object(reply_text=AsyncMock()))
        self.row=dict(inn='1234567891',username='retry',product='Право_ООО',full_name='Контакт',fields={})
    async def test_retry_keeps_prepared_items_and_never_starts_sending(self):
        self.setup_context()
        journal=Mock();journal.retry_rows.return_value=[self.row]
        new=dict(prepared_message_id=2,inn=self.row['inn'],username='retry',product='Право_ООО')
        with patch('bot.user_is_allowed',return_value=True),patch('legal_research.Journal',return_value=journal),\
             patch('arbitration_api.reset_service_block') as reset,patch('bot.save_import'),\
             patch('bot.prepare_one_client',new_callable=AsyncMock,return_value=('prepared','Готово',new)) as prepare:
            await bot.retry_research_command(self.update,self.ctx)
        prepare.assert_awaited_once_with(self.row);reset.assert_called_once()
        queue=self.ctx.application.bot_data[bot.QUEUE_KEY]
        self.assertEqual([x['prepared_message_id'] for x in queue['items']],[1,2])
        self.assertEqual(queue['state'],'awaiting_time');self.assertIsNone(queue['send_at'])
        self.assertNotIn(bot.SCHEDULE_TASK_KEY,self.ctx.application.bot_data)
    async def test_scheduled_send_and_unauthorized_actor_do_not_change_queue(self):
        for allowed,state in [(True,'scheduled'),(True,'sending'),(False,'awaiting_time')]:
            self.setup_context(state)
            with patch('bot.user_is_allowed',return_value=allowed),patch('legal_research.Journal') as journal:
                await bot.retry_research_command(self.update,self.ctx)
            journal.assert_not_called();self.assertIs(self.ctx.application.bot_data[bot.QUEUE_KEY],self.old)
    async def test_pending_new_excel_is_not_overwritten(self):
        self.setup_context();self.ctx.user_data['pending_rows']=[{'new':'file'}]
        with patch('bot.user_is_allowed',return_value=True),patch('legal_research.Journal') as journal:
            await bot.retry_research_command(self.update,self.ctx)
        journal.assert_not_called();self.assertEqual(self.ctx.user_data['pending_rows'],[{'new':'file'}])
