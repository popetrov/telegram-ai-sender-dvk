import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for name,value in dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='test',OPENAI_API_KEY='test',
                       BOT_TOKEN='123:test',MANAGER_ID='test').items():
    os.environ.setdefault(name,value)
import bot
from telethon.errors import FloodWaitError


class FloodWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = patch.object(bot,'DATA_DIR',self.root)
        self.data.start()
        self.addCleanup(self.data.stop)
        self.item = dict(prepared_status='prepared',inn='1',full_name='Test',username='test',
                         product='test',final_message='saved text',prompt_file='test.md',
                         prompt_text='saved prompt',model='test',raw_result='saved',prepared_message_id=1)

    async def test_spam_stop_reports_progress_and_never_attempts_next_recipient(self):
        queue = dict(send_at=datetime.now().astimezone(), state='scheduled', chat_id=42,
                     items=[dict(self.item,prepared_message_id=n) for n in (1,2,3)])
        app = SimpleNamespace(bot_data={bot.QUEUE_KEY:queue},bot=SimpleNamespace(send_message=AsyncMock()))
        with patch('access_runtime.require_access'), patch('schedule_state.save'), \
             patch('bot.asyncio.sleep',new_callable=AsyncMock), \
             patch('bot.send_one_prepared_message',new_callable=AsyncMock,
                   side_effect=[('sent','saved'),bot.TelegramSpamPause('PeerFloodError')]) as send:
            await bot.send_queue_at_time(app,queue)
        self.assertEqual(send.await_count,2)
        self.assertEqual(queue['state'],'interrupted')
        summary = app.bot.send_message.call_args.kwargs['text']
        self.assertIn('Успешно отправлено и записано: 1.',summary)
        self.assertIn('Текущая остановленная запись: 2/3.',summary)
        self.assertIn('Следующих получателей не обрабатывали: 1.',summary)

    async def test_resolver_limit_stops_queue_before_next_recipient(self):
        items=[self.item,dict(self.item,prepared_message_id=2,username='next')]
        queue=dict(send_at=datetime.now().astimezone(),state='scheduled',chat_id=42,items=items)
        app=SimpleNamespace(bot_data={bot.QUEUE_KEY:queue},bot=SimpleNamespace(send_message=AsyncMock()))
        with patch('access_runtime.require_access'), patch('bot.get_existing_client_keys',return_value=(set(),set())), \
             patch.object(bot.telegram_client,'get_entity',new_callable=AsyncMock,
                          side_effect=FloodWaitError(request=None,capture=1502)) as resolve, \
             patch.object(bot.telegram_client,'send_message',new_callable=AsyncMock) as send, \
             patch('schedule_state.save'), patch('bot.save_processed_client') as save:
            await bot.send_queue_at_time(app,queue)
        self.assertEqual(queue['state'],'interrupted')
        self.assertEqual(resolve.await_count,1)
        send.assert_not_awaited()
        save.assert_not_called()
        self.assertEqual(items[1]['prepared_status'],'prepared')

    async def test_peerflood_retry_resolver_failure_is_known_not_sent_and_stops(self):
        with ExitStack() as stack:
            stack.enter_context(patch('access_runtime.require_access'))
            stack.enter_context(patch('bot.is_peerflood_retry',return_value=True))
            stack.enter_context(patch('bot.start_peerflood_retry',return_value=(1,2,3)))
            stack.enter_context(patch.object(bot.telegram_client,'get_entity',new_callable=AsyncMock,
                                     side_effect=FloodWaitError(request=None,capture=1502)))
            send=stack.enter_context(patch.object(bot.telegram_client,'send_message',new_callable=AsyncMock))
            status=stack.enter_context(patch('bot.update_message_status'))
            stack.enter_context(patch('bot.update_client_status'))
            stack.enter_context(patch('bot.update_prepared_status'))
            with self.assertRaises(bot.TelegramSendPause):
                await bot.send_one_prepared_message(self.item)
        self.assertEqual(status.call_args.kwargs['status'],'not_sent')
        send.assert_not_awaited()

    async def test_cooldown_survives_restart_and_blocks_before_touching_database(self):
        with patch('bot.time.time',return_value=1000):
            bot.pause_for_flood(FloodWaitError(request=None,capture=1502))
        with patch('bot.time.time',return_value=1100),patch('access_runtime.require_access'), \
             patch('bot.get_existing_client_keys') as db:
            with self.assertRaises(bot.TelegramSendPause):
                await bot.send_one_prepared_message(self.item)
            db.assert_not_called()
        with patch('bot.time.time',return_value=2503):
            bot.require_send_cooldown_finished()

    async def test_unwritable_or_corrupt_cooldown_never_continues_queue(self):
        (self.root/'send_cooldown.json').write_text('{broken')
        with self.assertRaises(bot.TelegramSendPause):
            bot.require_send_cooldown_finished()
        self.assertIsInstance(bot.pause_for_flood(FloodWaitError(request=None,capture=60)),bot.TelegramSendPause)
