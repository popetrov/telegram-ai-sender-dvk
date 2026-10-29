import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from dotenv import dotenv_values
from authority_store import AuthorityStore
from owner_notifications import OwnerNotifications
from access_server import create_server
from center_runtime import run_center
from center_setup import configure_service


class SeparateBotsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthorityStore(self.root / 'authority.sqlite')
        self.notices = OwnerNotifications(self.store)
        self.signer = Ed25519PrivateKey.generate()

    def test_admin_has_no_lease_or_document_transport_handlers(self):
        from telegram.ext import MessageHandler
        app = create_server('123:admin',99,self.signer,self.store,role='admin',notices=self.notices)
        commands = set().union(*(getattr(h,'commands',set()) for h in app.handlers[0]))
        self.assertNotIn('lease',commands)
        self.assertIn('approve',commands)
        self.assertIn('companies',commands)
        self.assertFalse(any(isinstance(h,MessageHandler) for h in app.handlers[0]))
        self.assertEqual(app.bot.request.read_timeout,30)

    async def test_download_timeout_is_identified_and_retry_can_save_event(self):
        import sync_runtime
        import sync_protocol as protocol
        self.store.register(42,'m:42')
        self.store.assign(42,'dvk')
        cipher = X25519PrivateKey.generate()
        app = create_server('124:service',99,self.signer,self.store,cipher,role='service')
        from telegram.ext import MessageHandler
        callback = next(h.callback for h in app.handlers[0] if isinstance(h,MessageHandler))
        data = dict(installation='install',data={'table':'clients','row':{'id':1}})
        packet = protocol.encrypt(protocol.public_text(cipher),dict(subject='m:42',event_id='a'*32,data=data))
        remote = SimpleNamespace(download_as_bytearray=AsyncMock(side_effect=TimeoutError('do not log file URL')))
        document = SimpleNamespace(file_size=len(packet),get_file=AsyncMock(return_value=remote))
        message = SimpleNamespace(caption='DVK_SYNC_V1',document=document,reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                 effective_user=SimpleNamespace(id=42),effective_message=message)
        with self.assertLogs(sync_runtime.log,level='WARNING') as logs:
            await callback(update,None)
        self.assertIn('download',logs.output[0])
        self.assertNotIn('file URL',logs.output[0])
        message.reply_text.assert_not_awaited()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
        remote.download_as_bytearray.side_effect = None
        remote.download_as_bytearray.return_value = bytearray(packet)
        await callback(update,None)
        message.reply_text.assert_awaited_once()
        self.assertEqual(remote.download_as_bytearray.call_args.kwargs['read_timeout'],45)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_service_has_no_admin_commands(self):
        app = create_server('124:service',99,self.signer,self.store,X25519PrivateKey.generate(),
                            role='service',notices=self.notices)
        commands = set().union(*(getattr(h,'commands',set()) for h in app.handlers[0]))
        self.assertEqual(commands,{'lease','safety','research_status'})

    async def test_connection_notice_sent_only_once_and_via_admin_bot(self):
        self.store.register(42,'m:42')
        self.store.assign(42,'dvk')
        app = create_server('124:service',99,self.signer,self.store,role='service',notices=self.notices)
        callback = app.handlers[0][0].callback
        encoded = base64.urlsafe_b64encode(json.dumps(dict(subject='m:42',nonce='a'*48)).encode()).decode()
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                 effective_user=SimpleNamespace(id=42),effective_message=message)
        service_bot = SimpleNamespace(send_message=AsyncMock())
        for _ in range(3):
            await callback(update,SimpleNamespace(args=[encoded],bot=service_bot))
        admin_bot = SimpleNamespace(send_message=AsyncMock())
        await self.notices.flush(admin_bot,99)
        await OwnerNotifications(self.store).flush(admin_bot,99)
        service_bot.send_message.assert_not_awaited()
        admin_bot.send_message.assert_awaited_once()
        self.assertIn('подключён',admin_bot.send_message.call_args.args[1])

    async def test_new_launch_notifies_again_but_heartbeat_and_center_restart_do_not(self):
        self.store.register(42,'m:42')
        self.store.assign(42,'dvk')
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                 effective_user=SimpleNamespace(id=42),effective_message=message)
        admin = SimpleNamespace(send_message=AsyncMock())
        for launch in ['a'*32,'b'*32]:
            for _ in range(3):
                # Reconstruct the notifier/app to also simulate a center restart.
                notices = OwnerNotifications(AuthorityStore(self.store.path))
                app = create_server('124:service',99,self.signer,self.store,role='service',notices=notices)
                encoded = base64.urlsafe_b64encode(json.dumps(dict(
                    subject='m:42',nonce='c'*48,connection_id=launch)).encode()).decode()
                await app.handlers[0][0].callback(update,SimpleNamespace(args=[encoded]))
                await notices.flush(admin,99)
        self.assertEqual(admin.send_message.await_count,2)

    async def test_unapproved_launch_never_generates_connected_notice(self):
        app = create_server('124:service',99,self.signer,self.store,role='service',notices=self.notices)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                 effective_user=SimpleNamespace(id=42),effective_message=message)
        encoded = base64.urlsafe_b64encode(json.dumps(dict(subject='m:42',nonce='c'*48,connection_id='a'*32)).encode()).decode()
        await app.handlers[0][0].callback(update,SimpleNamespace(args=[encoded]))
        admin = SimpleNamespace(send_message=AsyncMock())
        await self.notices.flush(admin,99)
        admin.send_message.assert_awaited_once()
        self.assertIn('Ожидает',admin.send_message.call_args.args[1])

    async def test_payment_notice_once_per_expiration_and_dvk_exempt(self):
        self.store.company('buyer')
        with self.store.connection() as db:
            db.execute("UPDATE companies SET expires_at=100 WHERE id='buyer'")
        self.notices.payments(now=99)
        self.notices.payments(now=100)
        self.notices.payments(now=200)
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.notices.flush(bot,99)
        bot.send_message.assert_awaited_once()
        self.assertIn('buyer',bot.send_message.call_args.args[1])
        self.assertNotIn('dvk',bot.send_message.call_args.args[1])
        with self.store.connection() as db:
            db.execute("UPDATE companies SET expires_at=300 WHERE id='buyer'")
        self.notices.payments(now=301)
        await self.notices.flush(bot,99)
        self.assertEqual(bot.send_message.await_count,2)

    async def test_errors_are_limited_and_never_include_exception_text(self):
        for _ in range(10):
            self.notices.error('приём данных',ValueError('SECRET TOKEN client record'),42,now=100)
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.notices.flush(bot,99)
        bot.send_message.assert_awaited_once()
        self.assertNotIn('SECRET',bot.send_message.call_args.args[1])

    async def test_failed_owner_delivery_is_retained_for_retry(self):
        self.notices.once('one','Важное уведомление')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')))
        with self.assertRaises(RuntimeError):
            await self.notices.flush(bot,99)
        bot.send_message.side_effect = None
        await self.notices.flush(bot,99)
        await self.notices.flush(bot,99)
        self.assertEqual(bot.send_message.await_count,2)

    def test_local_service_setup_preserves_admin_token_and_state(self):
        path = self.root / '.env.authority'
        path.write_text('ACCESS_AUTHORITY_BOT_TOKEN=123:admin\nACCESS_AUTHORITY_ADMIN_ID=99\n',encoding='utf-8')
        with patch('builtins.input',return_value='dvk_sync2026_bot'), \
             patch('center_setup.getpass',return_value='124:service'):
            configure_service(self.root)
        values = dotenv_values(path)
        self.assertEqual(values['ACCESS_AUTHORITY_BOT_TOKEN'],'123:admin')
        self.assertEqual(values['ACCESS_SERVICE_BOT_TOKEN'],'124:service')
        self.assertEqual(values['ACCESS_SERVICE_BOT_USERNAME'],'dvk_sync2026_bot')

    def test_same_token_is_rejected_without_modifying_settings(self):
        path = self.root / '.env.authority'
        original = b'ACCESS_AUTHORITY_BOT_TOKEN=123:admin\n'
        path.write_bytes(original)
        with patch('builtins.input',return_value='dvk_sync2026_bot'), \
             patch('center_setup.getpass',return_value='123:admin'):
            with self.assertRaises(ValueError):
                configure_service(self.root)
        self.assertEqual(path.read_bytes(),original)

    async def test_both_bots_stop_cleanly_on_cancel(self):
        from unittest.mock import MagicMock
        apps = []
        for ident,name in [(1,'admin_bot'),(2,'dvk_sync2026_bot')]:
            app = MagicMock()
            app.bot = SimpleNamespace(id=ident,username=name)
            app.__aenter__ = AsyncMock(return_value=app)
            app.__aexit__ = AsyncMock(return_value=False)
            app.running = False
            app.start = AsyncMock(side_effect=lambda app=app:setattr(app,'running',True))
            app.stop = AsyncMock(side_effect=lambda app=app:setattr(app,'running',False))
            app.updater.running = False
            app.updater.start_polling = AsyncMock(side_effect=lambda app=app,**kw:setattr(app.updater,'running',True))
            app.updater.stop = AsyncMock(side_effect=lambda app=app:setattr(app.updater,'running',False))
            apps.append(app)
        async def idle(*args):
            await asyncio.Event().wait()
        notices = SimpleNamespace(run=idle)
        task = asyncio.create_task(run_center(*apps,notices,99,self.root,'dvk_sync2026_bot'))
        await asyncio.sleep(0.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for app in apps:
            app.updater.stop.assert_awaited_once()
            app.stop.assert_awaited_once()
            app.__aexit__.assert_awaited_once()
