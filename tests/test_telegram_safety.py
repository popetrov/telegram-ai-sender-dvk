import ast
import asyncio
import base64
import json
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from access_lease import canonical
from authority_store import AuthorityStore
from owner_notifications import OwnerNotifications
from safe_telegram import ProtectedTelegramClient, TelegramDeletionBlocked, _report_permission
from telegram_safety import SafetyJournal, SafetyNotifications, SafetyStop, register_receiver


class TelegramSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = SafetyJournal(self.root/'telegram-safety.sqlite')
        self.client = ProtectedTelegramClient(MemorySession(), 123, 'a'*32, safety_journal=self.journal)
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    async def test_sdk_dispatch_and_retries_never_send_destructive_requests(self):
        sender = SimpleNamespace(send=Mock())
        self.client._guard_sender(sender)
        # Check the whole batch before passing any member to the SDK.
        with self.assertRaises(TelegramDeletionBlocked):
            sender.send(item for item in [functions.updates.GetStateRequest(),
                                          functions.messages.DeleteMessagesRequest([11])])
        requests = [functions.messages.DeleteMessagesRequest([11,22], revoke=True),
                    functions.messages.DeleteHistoryRequest(types.InputPeerUser(77,0), max_id=0),
                    functions.channels.DeleteHistoryRequest(types.InputChannel(7,0), max_id=0),
                    functions.messages.SetHistoryTTLRequest(types.InputPeerUser(77,0), period=1),
                    functions.messages.UpdateDialogFilterRequest(1),
                    functions.folders.EditPeerFoldersRequest([])]
        for request in requests:
            with self.assertRaises(TelegramDeletionBlocked):
                sender.send(request)
            with self.assertRaises(TelegramDeletionBlocked):
                await self.client._call(sender, [request])
        sender.send.__closure__  # Dispatch is the checked wrapper, not a replaced mock.
        original = next(c.cell_contents for c in sender.send.__closure__ if isinstance(c.cell_contents, Mock))
        original.assert_not_called()
        with self.assertRaises(SafetyStop):
            sender.send(functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'client text'))
        original.assert_not_called()

    async def test_wrapper_and_message_shortcut_cannot_bypass_guard(self):
        request = functions.InvokeWithoutUpdatesRequest(functions.messages.DeleteMessagesRequest([11]))
        with patch.object(TelegramClient, '_call', new_callable=AsyncMock) as network:
            with self.assertRaises(TelegramDeletionBlocked):
                await self.client(request)
            message = types.Message(id=11, peer_id=types.PeerUser(77), message='secret customer text')
            message._client, message._input_chat = self.client, types.InputPeerUser(77,0)
            with self.assertRaises(TelegramDeletionBlocked):
                await message.delete()
            network.assert_not_awaited()
        self.assertNotIn('secret', self.journal.path.read_bytes().decode('latin1'))

    async def test_normal_reads_and_sends_work_until_incident_then_restart_stays_blocked(self):
        request = functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'hello')
        with patch.object(TelegramClient, '_call', new_callable=AsyncMock, return_value='ok') as network:
            self.assertEqual(await self.client(request),'ok')
            self.journal.block('DeleteMessagesRequest')
            restarted = ProtectedTelegramClient(MemorySession(),123,'a'*32,
                                                 safety_journal=SafetyJournal(self.journal.path))
            with self.assertRaises(SafetyStop):
                await restarted(request)
            self.assertEqual(network.await_count,1)
            self.assertEqual(await restarted(functions.updates.GetStateRequest()),'ok')

    async def test_disk_failure_still_blocks_request_and_subsequent_send(self):
        with patch.object(self.journal,'block',side_effect=OSError('disk full')):
            with self.assertRaises(TelegramDeletionBlocked):
                await self.client.delete_dialog(77)
        with self.assertRaises(SafetyStop):
            await self.client(functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'hello'))

    async def test_western_or_closed_console_never_changes_blocking_exception(self):
        class Western(io.StringIO):
            def write(self, text):
                text.encode('cp1252')
                return super().write(text)
        class Closed:
            def write(self, text):raise OSError('closed console')
            def flush(self):raise OSError('closed console')
        for stream in (Western(),Closed()):
            with patch('sys.stdout',stream):
                with self.assertRaises(TelegramDeletionBlocked):
                    await self.client.delete_messages(77,[11])
            with self.assertRaises(SafetyStop):self.journal.require_clear()

    async def test_retry_task_survives_western_console_and_recovers_notification_delivery(self):
        class Western(io.StringIO):
            def write(self,text):
                text.encode('cp1252')
                return super().write(text)
        self.journal.block('DeleteMessagesRequest')
        notify=AsyncMock()
        self.client.report_safety=AsyncMock(side_effect=[TimeoutError('PRIVATE_ERROR_TEXT'),None])
        worker=SafetyNotifications(self.client,notify,42,'manager_007','service_bot',self.public)
        cycles=0
        async def sleep(_):
            nonlocal cycles
            cycles+=1
            if cycles==2:raise asyncio.CancelledError()
        stream=Western()
        with patch('sys.stdout',stream),patch('telegram_safety.asyncio.sleep',side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):await worker.run()
        self.assertEqual(self.client.report_safety.await_count,2)
        self.assertEqual(self.journal.current()['owner_queued'],1)
        self.assertNotIn('PRIVATE_ERROR_TEXT',stream.getvalue())

    async def test_corrupt_safety_state_blocks_delivery(self):
        self.journal.path.write_bytes(b'broken')
        with self.assertRaises(SafetyStop):
            await self.client(functions.messages.SendMessageRequest(types.InputPeerUser(77,0),'hello'))

    async def test_unwritable_journal_still_notifies_both_parties_without_claiming_unconfirmed_deletion(self):
        with patch.object(self.journal,'connection',side_effect=OSError('disk full')):
            with self.assertRaises(TelegramDeletionBlocked):
                await self.client.delete_messages(77,[11])
        notify=AsyncMock();self.client.report_safety=AsyncMock()
        worker=SafetyNotifications(self.client,notify,42,'manager_007','service_bot',self.public)
        await worker.step();await worker.step()
        self.assertEqual(notify.await_count,1)
        self.assertEqual(self.client.report_safety.await_count,1)
        with self.assertRaises(SafetyStop):self.journal.require_clear()

    async def test_corruption_notice_is_distinct_from_prevented_deletion(self):
        self.journal.path.write_bytes(b'broken')
        notify=AsyncMock();self.client.report_safety=AsyncMock()
        worker=SafetyNotifications(self.client,notify,42,'manager_007','service_bot',self.public)
        await worker.step()
        self.assertIn('журнал безопасности',notify.call_args.args[0])
        self.assertNotIn('запрос заблокирован',notify.call_args.args[0])
        self.assertEqual(self.client.report_safety.call_args.args[1]['operation'],'safety_journal_unreadable')

    async def test_only_exact_incident_report_to_verified_bot_is_allowed_while_stopped(self):
        self.journal.block('DeleteHistoryRequest')
        text='/safety exact encoded report'
        token=_report_permission.set((99,text))
        try:
            self.client._check_request(functions.messages.SendMessageRequest(types.InputPeerUser(99,0),text))
            for peer,body in [(77,text),(99,'client message')]:
                with self.assertRaises(SafetyStop):
                    self.client._check_request(functions.messages.SendMessageRequest(types.InputPeerUser(peer,0),body))
            with self.assertRaises(TelegramDeletionBlocked):
                self.client._check_request(functions.messages.DeleteMessagesRequest([11]))
        finally:
            _report_permission.reset(token)

    async def test_notifications_retry_independently_and_stop_survives_delivery(self):
        row=self.journal.block('DeleteMessagesRequest')
        notify=AsyncMock(side_effect=[RuntimeError('offline'),None])
        self.client.report_safety=AsyncMock(side_effect=[RuntimeError('offline'),None])
        worker=SafetyNotifications(self.client,notify,42,'manager_007','service_bot',self.public)
        with self.assertRaises(RuntimeError):await worker.step()
        restarted=SafetyNotifications(self.client,notify,42,'manager_007','service_bot',self.public)
        await restarted.step()
        await restarted.step()
        self.assertEqual(notify.await_count,2)
        self.assertEqual(self.client.report_safety.await_count,2)
        state=self.journal.current()
        self.assertEqual(state['incident_id'],row['incident_id'])
        self.assertEqual((state['owner_queued'],state['manager_notified']),(1,1))
        with self.assertRaises(SafetyStop):self.journal.require_clear()

    def receiver_fixture(self, account=42, manager='manager_007'):
        store=AuthorityStore(self.root/'authority.sqlite')
        store.register(42,'manager_007:42');store.assign(42,'dvk')
        notices=OwnerNotifications(store)
        application=SimpleNamespace(add_handler=Mock())
        register_receiver(application,store,self.key,notices)
        callback=application.add_handler.call_args.args[0].callback
        report=dict(protocol='DVK_SAFETY_V1',incident_id='a'*32,manager_id=manager,operation='DeleteMessagesRequest')
        message=SimpleNamespace(reply_text=AsyncMock())
        update=SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                               effective_user=SimpleNamespace(id=account,is_bot=False),effective_message=message)
        context=SimpleNamespace(args=[base64.urlsafe_b64encode(canonical(report)).decode()])
        return store,notices,callback,update,context

    async def test_center_queues_one_owner_notice_before_signed_receipt_and_retries_flush(self):
        store,notices,callback,update,context=self.receiver_fixture()
        await callback(update,context);await callback(update,context)
        self.assertFalse(store.permission(42,'manager_007:42')[0])
        envelope=json.loads(update.effective_message.reply_text.call_args.args[0].split(' ',1)[1])
        self.key.public_key().verify(base64.b64decode(envelope['signature']),canonical(envelope['payload']))
        self.assertEqual(envelope['payload']['account_id'],42)
        with store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_notices').fetchone()[0],1)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('offline'),None]))
        with self.assertRaises(RuntimeError):await notices.flush(bot,99)
        await notices.flush(bot,99);await notices.flush(bot,99)
        self.assertEqual(bot.send_message.await_count,2)
        self.assertIn('заблокирован до отправки',bot.send_message.call_args.args[1])

    async def test_wrong_account_or_manager_cannot_report_under_someone_elses_identity(self):
        for account,manager in [(43,'manager_007'),(42,'manager_008')]:
            store,notices,callback,update,context=self.receiver_fixture(account,manager)
            await callback(update,context)
            self.assertTrue(store.permission(42,'manager_007:42')[0])
            update.effective_message.reply_text.assert_not_awaited()
            with store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_notices').fetchone()[0],0)

    async def test_real_report_adapter_requires_signed_ack_and_bot_identity(self):
        row=self.journal.block('DeleteMessagesRequest')
        report=dict(protocol='DVK_SAFETY_V1',incident_id=row['incident_id'],manager_id='manager_007',operation=row['operation'])
        payload=dict(protocol='DVK_SAFETY_ACK_V1',incident_id=row['incident_id'],account_id=42,manager_id='manager_007')
        envelope=dict(payload=payload,signature=base64.b64encode(self.key.sign(canonical(payload))).decode())
        conversation=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)),
                                     get_response=AsyncMock(return_value=SimpleNamespace(raw_text='DVK_SAFETY_ACK '+json.dumps(envelope))))
        manager=AsyncMock();manager.__aenter__.return_value=conversation
        self.client.conversation=Mock(return_value=manager)
        self.client.get_entity=AsyncMock(return_value=SimpleNamespace(id=99,username='service_bot',bot=True))
        await self.client.report_safety('service_bot',report,42,self.public)
        payload['account_id']=43
        envelope['signature']=base64.b64encode(self.key.sign(canonical(payload))).decode()
        conversation.get_response.return_value.raw_text='DVK_SAFETY_ACK '+json.dumps(envelope)
        with self.assertRaises(ValueError):await self.client.report_safety('service_bot',report,42,self.public)
        self.client.get_entity.return_value.bot=False
        with self.assertRaises(ValueError):await self.client.report_safety('service_bot',report,42,self.public)

    def test_all_packaged_clients_are_protected_and_cleanup_has_no_callers(self):
        from manual_update import RUNTIME
        root=Path(__file__).resolve().parents[1]
        for name in sorted(RUNTIME):
            if not name.endswith('.py'):continue
            tree=ast.parse((root/name).read_text('utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom) and node.module=='telethon':
                    if name!='safe_telegram.py':self.assertFalse(any(a.name=='TelegramClient' for a in node.names),name)
                if isinstance(node,ast.Call):
                    call=node.func
                    label=call.attr if isinstance(call,ast.Attribute) else call.id if isinstance(call,ast.Name) else ''
                    self.assertNotIn(label,{'delete_messages','delete_dialog','cleanup_service_messages','clean_service_history'},name)
