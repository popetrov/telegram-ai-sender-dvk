from types import SimpleNamespace as Object
import unittest
from unittest.mock import AsyncMock
from telegram.ext import CommandHandler,MessageHandler,CallbackQueryHandler,ApplicationHandlerStop
from bot_menu import register,publish,keyboard,PENDING


class App:
    def __init__(self):self.handlers={};self.bot=Object(set_my_commands=AsyncMock(),send_message=AsyncMock())
    def add_handler(self,handler,group=0):self.handlers.setdefault(group,[]).append(handler)


class MenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app=App();self.ctx=Object(args=None,user_data={})
        self.calls=[]
        async def execute(update,context):self.calls.append((update.effective_message.text,list(context.args)))
        for command in ('queue','spam_reset','approve','company_off','cancel'):
            self.app.add_handler(CommandHandler(command,execute))
        self.update=Object(effective_user=Object(id=42),effective_chat=Object(type='private'),
            effective_message=Object(text='',reply_text=AsyncMock()),callback_query=Object(
                data='',answer=AsyncMock(),edit_message_reply_markup=AsyncMock()))

    def setup_role(self,role):
        register(self.app,role=role,allowed=lambda u:u.effective_user.id==42 and u.effective_chat.type=='private')
        self.text=next(h.callback for h in self.app.handlers[-2] if isinstance(h,MessageHandler))

    async def press(self,value):
        self.update.effective_message.text=value
        with self.assertRaises(ApplicationHandlerStop):await self.text(self.update,self.ctx)

    async def test_queue_button_runs_existing_command_and_keeps_context_clean(self):
        self.setup_role('sender');await self.press('📋 Очередь')
        self.assertEqual(self.calls,[('/queue',[])])
        self.assertIsNone(self.ctx.args)

    async def test_normal_time_entry_is_not_eaten_by_menu(self):
        self.setup_role('sender');self.update.effective_message.text='20:30'
        await self.text(self.update,self.ctx)
        self.assertEqual(self.calls,[])

    async def test_other_person_and_group_cannot_use_owner_actions(self):
        self.setup_role('admin');self.update.effective_user.id=43
        await self.press('✅ Выдать доступ');self.assertEqual(self.calls,[])
        self.update.effective_user.id=42;self.update.effective_chat.type='group'
        await self.press('🚫 Отозвать доступ');self.assertEqual(self.calls,[])

    async def test_approve_wizard_passes_id_company_and_real_command_name(self):
        self.setup_role('admin');await self.press('✅ Выдать доступ')
        self.assertEqual(self.calls,[])
        await self.press('not an id')
        self.assertEqual(self.calls,[])
        await self.press('123456789 dvk')
        self.assertEqual(self.calls,[('/approve 123456789 dvk',['123456789','dvk'])])
        self.assertNotIn(PENDING,self.ctx.user_data)

    async def test_company_action_keeps_company_off_command_semantics(self):
        self.setup_role('admin');await self.press('⛔ Отключить компанию');await self.press('dvk')
        self.assertEqual(self.calls,[('/company_off dvk',['dvk'])])

    async def test_spam_reset_needs_explicit_confirmation_and_cannot_be_replayed(self):
        self.setup_role('sender');await self.press('🔓 Снять локальную паузу')
        self.assertEqual(self.calls,[])
        callback=next(h.callback for h in self.app.handlers[-2] if isinstance(h,CallbackQueryHandler))
        self.update.callback_query.data='menu:spam_checked'
        await callback(self.update,self.ctx)
        self.assertEqual(self.calls,[('/spam_reset ПРОВЕРЕНО',['ПРОВЕРЕНО'])])
        await callback(self.update,self.ctx)
        self.assertEqual(len(self.calls),1)

    async def test_sender_cannot_obtain_admin_menu_by_typing_admin_button(self):
        self.setup_role('sender');self.update.effective_message.text='✅ Выдать доступ'
        await self.text(self.update,self.ctx);self.assertEqual(self.calls,[])

    async def test_menu_cancels_unfinished_input(self):
        self.setup_role('admin');await self.press('✅ Выдать доступ');await self.press('🏠 Меню')
        self.assertNotIn(PENDING,self.ctx.user_data)
        self.assertIsNotNone(self.update.effective_message.reply_text.call_args.kwargs['reply_markup'])

    async def test_publish_installs_chat_scoped_command_menu_and_persistent_buttons(self):
        await publish(self.app,42,'sender')
        self.assertEqual(self.app.bot.set_my_commands.call_args.kwargs['scope'].chat_id,42)
        self.assertTrue(self.app.bot.send_message.call_args.kwargs['reply_markup'].is_persistent)
        self.assertIn('queue',[c.command for c in self.app.bot.set_my_commands.call_args.args[0]])


if __name__=='__main__':unittest.main()
