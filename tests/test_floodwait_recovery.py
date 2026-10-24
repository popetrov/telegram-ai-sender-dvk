import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

for name,value in dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='test',OPENAI_API_KEY='test',
                       BOT_TOKEN='123:test',MANAGER_ID='test').items():
    os.environ.setdefault(name,value)
import config
import database
import bot

ERROR='FloodWaitError: A wait of 1502 seconds is required (caused by ResolveUsernameRequest). Сообщение не отправлялось.'


class FloodWaitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for change in (patch.object(config,'DATABASE_PATH',str(self.root/'local.sqlite')),
                       patch.object(config,'MANAGER_ID','test'),patch.object(bot,'DATA_DIR',self.root)):
            change.start();self.addCleanup(change.stop)
        database.init_database()
        self.fields=dict(inn='1234567890',full_name='Test',username='recipient',product='BG',
                         prompt_file='bg.md',prompt_text='saved prompt',model='saved model',
                         raw_result='saved research',final_message='saved final text')
        self.prepared=database.save_prepared_message(**self.fields)
        self.client,self.research,self.message=database.save_processed_client(**self.fields,status='manual_review')
        database.update_message_status(self.message,'not_sent',send_error=ERROR)
        database.update_prepared_message_status(self.prepared,'manual_review',ERROR)

    def test_restores_only_verified_unsent_and_claim_is_single_use_without_new_research(self):
        rows=database.get_pending_prepared_messages()
        self.assertEqual([r['id'] for r in rows],[self.prepared])
        before=rows[0]['final_message']
        client,research,message=database.start_floodwait_retry(prepared_message_id=self.prepared,**self.fields)
        self.assertEqual((client,research),(self.client,self.research))
        self.assertNotEqual(message,self.message)
        with database.get_connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_runs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT final_message FROM prepared_messages').fetchone()[0],before)
        self.assertEqual(database.get_pending_prepared_messages(),[])
        with self.assertRaises(ValueError):
            database.start_floodwait_retry(prepared_message_id=self.prepared,**self.fields)

    def test_sent_sending_and_unknown_outcomes_are_never_recovered(self):
        for status,error in [('sent',None),('sending',None),('uncertain','TimeoutError: unknown'),
                             ('not_sent','FloodWaitError: from SendMessageRequest')]:
            database.update_message_status(self.message,status,send_error=error)
            self.assertEqual(database.get_pending_prepared_messages(),[])
            with self.assertRaises(ValueError):
                database.start_floodwait_retry(prepared_message_id=self.prepared,**self.fields)

    def test_older_ambiguous_or_sent_attempt_blocks_retry_even_if_latest_says_not_sent(self):
        with database.get_connection() as db:
            db.execute('''INSERT INTO messages(id,client_id,research_run_id,text,status,send_error,created_at)
                VALUES (0,?,?,?,'uncertain','TimeoutError: unknown','earlier')''',
                (self.client,self.research,self.fields['final_message']))
        self.assertEqual(database.get_pending_prepared_messages(),[])

    def test_changed_text_and_forged_label_without_matching_history_are_rejected(self):
        fields={**self.fields,'final_message':'different'}
        with self.assertRaises(ValueError):
            database.start_floodwait_retry(prepared_message_id=self.prepared,**fields)
        database.update_prepared_message_status(self.prepared,'manual_review',ERROR+'different')
        self.assertEqual(database.get_pending_prepared_messages(),[])

    async def test_actual_bot_path_reuses_text_and_never_calls_gpt_or_ordinary_duplicate_creation(self):
        item=bot.item_from_saved_record(database.get_pending_prepared_messages()[0])
        with patch('access_runtime.require_access'), \
             patch.object(bot.telegram_client,'get_entity',new_callable=AsyncMock,return_value=SimpleNamespace(id=101)), \
             patch.object(bot.telegram_client,'send_message',new_callable=AsyncMock,return_value=SimpleNamespace(id=501)) as send, \
             patch('bot.generate_company_analysis') as generate,patch('bot.save_processed_client') as create:
            status,_=await bot.send_one_prepared_message(item)
        self.assertEqual(status,'sent')
        self.assertEqual(send.call_args.args[1],self.fields['final_message'])
        generate.assert_not_called();create.assert_not_called()
        self.assertEqual(database.get_pending_prepared_messages(),[])

    async def test_queue_command_reports_verified_floodwait_rows_separately(self):
        update=SimpleNamespace(effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(id=42),
                               effective_message=SimpleNamespace(reply_text=AsyncMock()))
        context=SimpleNamespace(application=SimpleNamespace(bot_data={}))
        with patch('bot.user_is_allowed',return_value=True),patch('bot.queue_is_active',return_value=False):
            await bot.queue_command(update,context)
        self.assertEqual(len(context.application.bot_data[bot.QUEUE_KEY]['items']),1)
        self.assertIn('FloodWait): 1',update.effective_message.reply_text.call_args.args[0])
