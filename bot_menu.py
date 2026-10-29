"""Persistent human-readable buttons dispatch existing authorized command handlers."""
import re
from types import SimpleNamespace
from telegram import ReplyKeyboardMarkup,InlineKeyboardButton,InlineKeyboardMarkup,BotCommand,BotCommandScopeChat
from telegram.ext import MessageHandler,CommandHandler,CallbackQueryHandler,ApplicationHandlerStop,filters

SENDER={
    '📋 Очередь':('queue','Восстановить очередь'),
    '✍️ Подготовить сообщения':('confirm','Подготовить сообщения из Excel'),
    '🔄 Повторить исследования':('retry_research','Повторить только незавершённые исследования'),
    '🛑 Отменить очередь':('cancel','Отменить запланированную очередь'),
    '🔑 Проверить доступ':('access','Проверить центральный доступ'),
    '💬 Диалоги':('dialogues','Диалоги и ручное управление'),
    '📨 Запросить доступ':('request_access','Запросить доступ'),
    '🔓 Снять локальную паузу':('spam_reset','Подтвердить проверку ограничения'),
    '📎 Загрузить Excel':('upload','Как загрузить Excel'),
    '❓ Помощь':('start','Инструкция'),
    '🏠 Меню':('menu','Показать кнопки'),
}
ADMIN={
    '🛡 Контроль переписки':('monitor','Контроль работы программ менеджеров'),
    '👥 Менеджеры':('users','Список менеджеров'),
    '🏢 Компании':('companies','Список компаний'),
    '✅ Выдать доступ':('approve','Выдать доступ менеджеру'),
    '🚫 Отозвать доступ':('revoke','Отозвать доступ менеджера'),
    '➕ Создать / включить компанию':('company','Создать или включить компанию'),
    '⛔ Отключить компанию':('company_off','Отключить компанию'),
    '📅 Продлить оплату':('renew','Продлить компанию на месяц и 2 дня'),
    '🪪 Карточка сотрудника':('profile','Заполнить внутреннюю карточку'),
    '↩️ Отменить ввод':('cancel','Отменить ввод карточки'),
    '📊 Статус центра':('start','Статус центра'),
    '🏠 Меню':('menu','Показать кнопки'),
}
PROMPTS={
    'approve':'Введите Telegram ID и ID компании через пробел. Например: 123456789 dvk.\nID компании можно посмотреть кнопкой «Компании».',
    'revoke':'Введите Telegram ID менеджера, которому нужно отозвать доступ.',
    'profile':'Введите Telegram ID сотрудника для внутренней карточки.',
    'company':'Введите ID компании, которую нужно создать или включить. Например: dvk.',
    'company_off':'Введите ID компании, которую нужно отключить.',
    'renew':'Введите ID компании, которой нужно продлить оплату.',
}
PENDING='button_menu_pending'


def sender_actions():
    """Keep other businesses' menus unchanged until this manager actually uses legal research."""
    import sqlite3
    from pathlib import Path
    import config
    available=False
    path=Path(config.DATABASE_PATH)
    if path.is_file():
        db=None
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='legal_research_jobs'").fetchone():
                available=bool(db.execute('SELECT 1 FROM legal_research_jobs WHERE manager=? LIMIT 1',(config.MANAGER_ID,)).fetchone())
        except sqlite3.Error:pass
        finally:
            if db is not None:db.close()
    return {label:value for label,value in SENDER.items() if available or value[0]!='retry_research'}


def keyboard(role='sender'):
    labels=list(ADMIN if role=='admin' else sender_actions())
    return ReplyKeyboardMarkup([labels[i:i+2] for i in range(0,len(labels),2)],resize_keyboard=True,is_persistent=True)


class MessageProxy:
    def __init__(self,message,text):self.original,self.text=message,text
    def __getattr__(self,name):return getattr(self.original,name)


class UpdateProxy:
    def __init__(self,update,text):
        self.original=update;self.effective_message=MessageProxy(update.effective_message,text);self.message=self.effective_message
    def __getattr__(self,name):return getattr(self.original,name)


async def dispatch(application,update,context,command,args=()):
    """Preserve every existing ownership/access check inside the real handler."""
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler,CommandHandler) and command in handler.commands:
                previous=getattr(context,'args',None);context.args=list(args)
                try:return await handler.callback(UpdateProxy(update,'/'+command+(' '+' '.join(args) if args else '')),context)
                finally:context.args=previous
    await update.effective_message.reply_text('Эта функция пока не подключена в вашей установке.',reply_markup=keyboard())


def register(application,*,role,allowed):
    actions=ADMIN if role=='admin' else SENDER

    async def menu(update,context):
        if not allowed(update):return
        context.user_data.pop(PENDING,None)
        await update.effective_message.reply_text('Выберите действие кнопками под полем ввода.',reply_markup=keyboard(role))

    async def text(update,context):
        value=update.effective_message.text or ''
        pending=context.user_data.get(PENDING)
        if value not in actions and not pending:return
        if not allowed(update):raise ApplicationHandlerStop
        if value in actions:
            context.user_data.pop(PENDING,None)
            command=actions[value][0]
            if command=='menu':await menu(update,context)
            elif command=='upload':
                await update.effective_message.reply_text('Нажмите скрепку 📎 и отправьте Excel-файл .xlsx в этот чат. После проверки нажмите «Подготовить сообщения».',reply_markup=keyboard(role))
            elif command=='spam_reset':
                context.user_data[PENDING]='spam_reset'
                await update.effective_message.reply_text(
                    'Проверьте ограничения аккаунта в @SpamBot и возможность отправки. Если проверили — подтвердите ниже. '
                    'Это снимет только локальную остановку программы. Включённые автоответы и напоминания смогут продолжиться; очередь первых сообщений запускается отдельно.',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Открыть SpamBot',url='https://t.me/SpamBot')],
                        [InlineKeyboardButton('Проверил — снять локальную паузу',callback_data='menu:spam_checked')],
                        [InlineKeyboardButton('Оставить на паузе',callback_data='menu:keep_pause')]]))
            elif role=='admin' and command in PROMPTS:
                context.user_data[PENDING]=command
                await update.effective_message.reply_text(PROMPTS[command]+'\nДля выхода нажмите «Меню».')
            else:await dispatch(application,update,context,command)
        elif pending=='spam_reset':
            await update.effective_message.reply_text('Используйте кнопку подтверждения выше или нажмите «Меню».')
        else:
            args=value.strip().split()
            valid=(len(args)==2 and args[0].isdigit() and int(args[0])>0) if pending=='approve' else (
                len(args)==1 and args[0].isdigit() and int(args[0])>0 if pending in {'revoke','profile'} else
                len(args)==1 and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',args[0]) is not None)
            if not valid:
                await update.effective_message.reply_text('Проверьте введённые данные.\n'+PROMPTS[pending])
            else:
                context.user_data.pop(PENDING,None)
                await dispatch(application,update,context,pending,args)
        raise ApplicationHandlerStop

    async def confirmation(update,context):
        if not allowed(update):
            await update.callback_query.answer('Доступ запрещён.',show_alert=True);return
        if context.user_data.get(PENDING)!='spam_reset':
            await update.callback_query.answer('Кнопка устарела. Откройте действие заново.',show_alert=True);return
        context.user_data.pop(PENDING,None)
        if update.callback_query.data=='menu:spam_checked':
            await update.callback_query.answer('Проверка подтверждена.')
            await dispatch(application,update,context,'spam_reset',['ПРОВЕРЕНО'])
        else:await update.callback_query.answer('Пауза сохранена.')
        await update.callback_query.edit_message_reply_markup(reply_markup=None)

    async def clear_input(update,context):
        if allowed(update):context.user_data.pop(PENDING,None)

    application.add_handler(MessageHandler(filters.COMMAND,clear_input),group=-4)
    application.add_handler(CommandHandler(['menu','help'],menu),group=-3)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text),group=-2)
    if role=='sender':application.add_handler(CallbackQueryHandler(confirmation,pattern=r'^menu:(spam_checked|keep_pause)$'),group=-2)


async def publish(application,account_id,role):
    """Install Telegram's slash-menu too and show the keyboard after app startup."""
    actions=ADMIN if role=='admin' else sender_actions()
    unique={command:description for command,description in actions.values() if command!='upload'}
    unique['menu']='Показать кнопки'
    await application.bot.set_my_commands([BotCommand(k,v) for k,v in unique.items()],scope=BotCommandScopeChat(account_id))
    await application.bot.send_message(chat_id=account_id,text='Кнопки управления готовы — выберите действие под полем ввода.',reply_markup=keyboard(role))
