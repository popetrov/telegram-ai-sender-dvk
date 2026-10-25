"""Manager-only controls. These callbacks never send messages to a customer."""
import re
from dialogue_store import StaleDialogue


def controls(thread_id,revision,state):
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup
    if state in {'opt_out','closed'}:return None
    command,label=('r','Вернуть боту') if state=='human' else ('p','Взять диалог на себя')
    return InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data=f'dlg:{command}:{thread_id}:{revision}')]])


def register(application,store,*,account_id,manager_id):
    from telegram.ext import CallbackQueryHandler,CommandHandler

    async def control(update,context):
        query=update.callback_query
        if (update.effective_user.id!=account_id or update.effective_chat.type!='private'):
            await query.answer('Доступ запрещён.',show_alert=True);return
        match=re.fullmatch(r'dlg:([pr]):([0-9a-f]{32}):(\d+)',query.data or '')
        if not match:
            await query.answer('Неизвестная команда.',show_alert=True);return
        action,thread_id,revision=match.groups()
        try:
            store.manager_control(thread_id,actor_account_id=account_id,manager_id=manager_id,
                                  command='pause' if action=='p' else 'resume',expected_revision=int(revision))
            thread,_=store.snapshot(thread_id)
            await query.answer('Диалог у вас.' if action=='p' else 'Автоответы включены; прежние ответы не повторяются.')
            await query.edit_message_reply_markup(reply_markup=controls(thread_id,thread['revision'],thread['state']))
        except StaleDialogue:
            thread,_=store.snapshot(thread_id)
            await query.answer('Состояние диалога изменилось. Кнопка обновлена.',show_alert=True)
            await query.edit_message_reply_markup(reply_markup=controls(thread_id,thread['revision'],thread['state']))
        except (ValueError,PermissionError):
            await query.answer('Это действие недоступно для данного диалога.',show_alert=True)

    application.add_handler(CallbackQueryHandler(control,pattern=r'^dlg:'))
    async def status(update,context):
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':return
        threads=store.threads(account_id)
        if not threads:
            await update.effective_message.reply_text('Подключённых диалогов пока нет.');return
        args=getattr(context,'args',None) or []
        try:page=int(args[0]) if args else 1
        except (ValueError,TypeError):page=1
        pages=max(1,(len(threads)+9)//10);page=max(1,min(page,pages))
        if len(threads)>10:
            paused=sum(t['state'] in {'human','opt_out','closed'} for t in threads)
            await update.effective_message.reply_text(
                f'Диалогов: {len(threads)}. Включены: {len(threads)-paused}. На паузе или завершены: {paused}.\n'
                f'Страница {page}/{pages}. Листайте кнопками под последним диалогом.')
        selected=threads[(page-1)*10:page*10]
        for index,thread in enumerate(selected):
            state=('на паузе' if thread['state']=='human' else 'запрещены клиентом' if thread['state']=='opt_out'
                   else 'завершены' if thread['state']=='closed' else 'проверяется история' if not thread['history_complete'] else 'включены')
            markup=controls(thread['id'],thread['revision'],thread['state'])
            if pages>1 and index==len(selected)-1:
                from telegram import InlineKeyboardButton,InlineKeyboardMarkup
                buttons=list(markup.inline_keyboard) if markup else []
                navigation=[]
                if page>1:navigation.append(InlineKeyboardButton('← Назад',callback_data=f'dlgp:{page-1}'))
                if page<pages:navigation.append(InlineKeyboardButton('Дальше →',callback_data=f'dlgp:{page+1}'))
                buttons.append(navigation);markup=InlineKeyboardMarkup(buttons)
            await update.effective_message.reply_text(
                f"{store.client_label(thread['id'])} · {thread['product']}\nАвтоответы: {state}.",
                reply_markup=markup)
    application.add_handler(CommandHandler('dialogues',status))
    async def page(update,context):
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':
            await update.callback_query.answer('Доступ запрещён.',show_alert=True);return
        await update.callback_query.answer()
        from bot_menu import dispatch
        await dispatch(application,update,context,'dialogues',[update.callback_query.data.split(':')[1]])
    application.add_handler(CallbackQueryHandler(page,pattern=r'^dlgp:[0-9]+$'))


async def deliver_notices(bot,store,*,account_id):
    for notice in store.pending_notices(account_id):
        text=(f"Клиент: {store.client_label(notice['thread_id'])} · {notice['product']}\n"
              f"{notice['summary']}\n\n"
              + ('Автоответы приостановлены.' if notice['state']=='human' else 'Можете подключиться к диалогу.'))
        await bot.send_message(chat_id=account_id,text=text,
                               reply_markup=controls(notice['thread_id'],notice['revision'],notice['state']))
        store.acknowledge_notice(notice['id'],account_id=account_id)
