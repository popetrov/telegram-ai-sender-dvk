"""Manager-only controls. These callbacks never send messages to a customer."""
import re
from dialogue_store import StaleDialogue


def controls(thread_id,revision,state,*,thread=None,expanded=False):
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup
    if thread and thread.get('seller_enabled'):
        from dialogue_seller import MODE_LABELS
        username=thread.get('peer_username','')
        open_chat=([InlineKeyboardButton('Открыть переписку',url='https://t.me/'+username)]
                   if re.fullmatch(r'[A-Za-z0-9_]{1,32}',username) else [])
        if state in {'opt_out','closed'}:return InlineKeyboardMarkup([open_chat]) if open_chat else None
        codes={'active_negotiation':'a','nurture':'n','silent':'s','copilot':'c','paused_until':'p','off':'o'}
        human=(state=='human' or thread['mode'] in {'copilot','off','paused_until'}) and not thread['resume_pending']
        code,label=('a','Вернуть ИИ') if human else ('c','Взять на себя · стоп ИИ')
        rows=[[InlineKeyboardButton(label,callback_data=f'dlgm:{code}:{thread_id}:{revision}')]]
        if open_chat:rows.append(open_chat)
        rows.append([InlineKeyboardButton('Заявки клиента',callback_data=f'apps:{thread_id}')])
        rows.append([InlineKeyboardButton('Календарь и прогрев',callback_data=f'cal:t:{thread_id}')])
        if expanded:
            rows += [[InlineKeyboardButton(MODE_LABELS[mode],callback_data=f'dlgm:{value}:{thread_id}:{revision}')
                      for mode,value in list(codes.items())[i:i+2]] for i in range(0,6,2)]
        rows.append([InlineKeyboardButton('Скрыть режимы' if expanded else 'Все режимы',
                     callback_data=f'dlgv:{"c" if expanded else "e"}:{thread_id}:{revision}')])
        return InlineKeyboardMarkup(rows)
    if state in {'opt_out','closed'}:return None
    command,label=('r','Вернуть боту') if state=='human' else ('p','Взять диалог на себя')
    return InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data=f'dlg:{command}:{thread_id}:{revision}')]])


def dialogue_card(store,thread,*,summary=None,event_type=None):
    """The same ownership/status wording in lists, handoffs and button receipts."""
    tid=thread['id'];product=thread.get('active_product') or thread['product']
    if thread['state'] in {'opt_out','closed'}:
        ownership='ИИ не пишет. Контакт закрыт или действует запрет клиента.'
    elif thread['mode']=='off':ownership='Выключен: ИИ не анализирует и не пишет.'
    elif thread['resume_pending']:ownership='Проверяю переписку перед возвратом ИИ. Пока ИИ не пишет.'
    elif thread['mode']=='paused_until':ownership='Пауза: ИИ не пишет до указанного срока.'
    elif thread['state']=='human' or thread['mode']=='copilot':ownership='Сейчас отвечаете ВЫ (МОП). ИИ не пишет.'
    else:ownership='Сейчас отвечает ИИ. Ваш ручной ответ сразу остановит его.'
    text=f"{store.client_label(tid)} · {product}\n{ownership}"
    if thread.get('control_reason'):text+='\nПричина: '+thread['control_reason']
    if thread.get('seller_enabled'):
        from dialogue_memory import snapshot
        labels={'amount':'Сумма','purpose':'Цель','term':'Срок','deadline':'Когда нужно',
                'banks':'Банки','security':'Обеспечение','contract':'Контракт','product':'Продукт'}
        facts=[f for f in snapshot(store,tid) if f['category']=='need' and f['field'] in labels]
        if facts:
            text+='\n\nИз слов клиента:\n'+'\n'.join(labels[f['field']]+': '+f['value'][:160]
                +' (сообщение '+str(f['source_message_id'])+')' for f in facts[:8])
    business_events={'ready','need','call','documents','copilot','communication_task'}
    if not summary or event_type not in business_events:
        with store.connection() as db:
            last=db.execute("SELECT summary,event_type FROM dialogue_notices WHERE thread_id=? AND event_type IN "
                "('ready','need','call','documents','copilot','communication_task') ORDER BY created_at DESC,rowid DESC LIMIT 1",(tid,)).fetchone()
        if last:summary,event_type=last['summary'],last['event_type']
    if summary and event_type in business_events:text+='\n\nПотребность и договорённости:\n'+summary
    if event_type=='need' and thread['state']!='human' and thread['mode'] not in {'off','paused_until'}:
        text+='\n\nЭто информация о потребности, а не передача диалога. ИИ продолжает выяснять задачу.'
    if thread['state']=='human' or thread['mode']=='copilot':
        text+='\n\nЧто дальше: откройте переписку и ответьте клиенту. Вернуть ИИ можно только кнопкой «Вернуть ИИ».'
        if not thread.get('peer_username'):text+='\nУ контакта нет публичной ссылки; откройте его чат в Telegram вручную.'
    from dialogue_calendar_ui import status_line
    line=status_line(store,thread)
    if line:text+='\n\n'+line
    return text[:4000]


async def edit_card(query,store,thread):
    markup=controls(thread['id'],thread['revision'],thread['state'],thread=thread)
    if thread['seller_enabled'] and hasattr(query,'edit_message_text'):
        await query.edit_message_text(text=dialogue_card(store,thread),reply_markup=markup)
    else:await query.edit_message_reply_markup(reply_markup=markup)


def register(application,store,*,account_id,manager_id,seller_only=False):
    from telegram.ext import CallbackQueryHandler,CommandHandler
    from dialogue_calendar_ui import register as register_calendar
    register_calendar(application,store,account_id=account_id,manager_id=manager_id)

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
            await query.answer('Диалог у вас.' if action=='p' else
                ('Проверяю свежую переписку перед возвратом.' if thread['seller_enabled'] else 'Автоответы включены; прежние ответы не повторяются.'))
            await edit_card(query,store,thread)
        except StaleDialogue:
            thread,_=store.snapshot(thread_id)
            await query.answer('Состояние диалога изменилось. Кнопка обновлена.',show_alert=True)
            await edit_card(query,store,thread)
        except (ValueError,PermissionError):
            await query.answer('Это действие недоступно для данного диалога.',show_alert=True)

    application.add_handler(CallbackQueryHandler(control,pattern=r'^dlg:'))
    async def status(update,context):
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':return
        threads=store.threads(account_id)
        if seller_only:threads=[t for t in threads if t['seller_enabled']]
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
            markup=controls(thread['id'],thread['revision'],thread['state'],thread=thread)
            if pages>1 and index==len(selected)-1:
                from telegram import InlineKeyboardButton,InlineKeyboardMarkup
                buttons=list(markup.inline_keyboard) if markup else []
                navigation=[]
                if page>1:navigation.append(InlineKeyboardButton('← Назад',callback_data=f'dlgp:{page-1}'))
                if page<pages:navigation.append(InlineKeyboardButton('Дальше →',callback_data=f'dlgp:{page+1}'))
                buttons.append(navigation);markup=InlineKeyboardMarkup(buttons)
            if thread['seller_enabled']:
                from dialogue_seller import MODE_LABELS
                state=MODE_LABELS[thread['mode']]+(' — проверяется переписка' if thread['resume_pending'] else '')
                if thread['state'] in {'opt_out','closed'}:state='Исходящие запрещены: отказ клиента или закрытая история'
                if thread['mode'] in {'nurture','silent'}:state+=' (см. календарь)'
            await update.effective_message.reply_text(
                dialogue_card(store,thread) if thread['seller_enabled'] else
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

    async def mode_control(update,context):
        from dialogue_seller import SellerControls,MODE_LABELS
        from telegram import ForceReply
        query=update.callback_query
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':
            await query.answer('Доступ запрещён.',show_alert=True);return
        match=re.fullmatch(r'dlgm:([anscpo]):([0-9a-f]{32}):(\d+)',query.data or '')
        if not match:
            await query.answer('Неизвестная команда.',show_alert=True);return
        code,thread_id,revision=match.groups();revision=int(revision)
        modes=dict(a='active_negotiation',n='nurture',s='silent',c='copilot',p='paused_until',o='off')
        try:
            if code in {'n','s'}:
                from dialogue_calendar_ui import show_groups
                await query.answer()
                await show_groups(query.message,store,thread_id,account_id,manager_id)
                return
            if code=='p':
                t=store.thread(thread_id)
                if t['account_id']!=account_id or t['manager_id']!=manager_id or not t['seller_enabled']:
                    raise PermissionError()
                # Pause immediately while asking a date. Failure to reply stays safe.
                SellerControls(store).set_mode(thread_id,'off',actor=account_id,manager=manager_id,revision=revision)
                prompt=await query.message.reply_text('До какого времени приостановить? Ответьте на это сообщение: '
                    'ДД.ММ.ГГГГ ЧЧ:ММ (московское время). Пока диалог выключен.',reply_markup=ForceReply(selective=True))
                context.user_data['seller_pause']=(thread_id,store.thread(thread_id)['revision'],prompt.message_id)
            else:
                SellerControls(store).set_mode(thread_id,modes[code],actor=account_id,manager=manager_id,revision=revision)
            t=store.thread(thread_id)
            text='Проверю переписку перед ответом.' if t['resume_pending'] else MODE_LABELS[t['mode']]
            if code in {'n','s'}:text+=' Плановые касания пока выключены.'
            await query.answer(text)
            await edit_card(query,store,t)
        except StaleDialogue:
            await query.answer('Карточка обновлена. Проверьте статус и при необходимости нажмите «Вернуть ИИ» ещё раз.',show_alert=True)
            await edit_card(query,store,store.thread(thread_id))
        except (ValueError,PermissionError):
            await query.answer('Режим недоступен: проверьте запрет клиента или результат отправки.',show_alert=True)
    application.add_handler(CallbackQueryHandler(mode_control,pattern=r'^dlgm:'))

    async def pause_reply(update,context):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from telegram.ext import ApplicationHandlerStop
        from dialogue_seller import SellerControls
        pending=context.user_data.get('seller_pause')
        message=update.effective_message
        if (not pending or update.effective_user.id!=account_id or update.effective_chat.type!='private'
                or not message.reply_to_message or message.reply_to_message.message_id!=pending[2]):return
        try:
            until=int(datetime.strptime(message.text.strip(),'%d.%m.%Y %H:%M').replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp())
            SellerControls(store).set_mode(pending[0],'paused_until',actor=account_id,manager=manager_id,revision=pending[1],until=until)
            context.user_data.pop('seller_pause',None)
            await message.reply_text('Пауза сохранена. Перед возвратом бот перечитает переписку; готовые старые ответы не отправит.')
        except (ValueError,StaleDialogue,PermissionError):
            await message.reply_text('Не удалось поставить паузу. Нужна будущая дата ДД.ММ.ГГГГ ЧЧ:ММ; '
                'если диалог изменился, выберите паузу заново. Текущие ограничения сохранены.')
        raise ApplicationHandlerStop
    from telegram.ext import MessageHandler,filters
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND,pause_reply),group=-5)

    async def view_modes(update,context):
        query=update.callback_query
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':
            await query.answer('Доступ запрещён.',show_alert=True);return
        match=re.fullmatch(r'dlgv:([ec]):([0-9a-f]{32}):(\d+)',query.data or '')
        if not match:return
        expanded,tid,_=match.groups();t=store.thread(tid)
        if t['account_id']!=account_id or t['manager_id']!=manager_id or not t['seller_enabled']:
            await query.answer('Доступ запрещён.',show_alert=True);return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=controls(tid,t['revision'],t['state'],thread=t,expanded=expanded=='e'))
    application.add_handler(CallbackQueryHandler(view_modes,pattern=r'^dlgv:'))

    async def application_cards(update,context):
        from dialogue_applications import applications,SUCCESS
        from telegram import InlineKeyboardButton,InlineKeyboardMarkup
        query=update.callback_query
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':
            if query:await query.answer('Доступ запрещён.',show_alert=True)
            return
        tid=query.data.split(':')[1] if query else None
        threads=[t for t in store.threads(account_id) if t['manager_id']==manager_id and t['seller_enabled'] and (tid is None or t['id']==tid)]
        if query:await query.answer()
        found=False
        for thread in threads:
            for app in applications(store,thread['id']):
                found=True
                text=(store.client_label(thread['id'])+' · '+app['product']+'\nЗаявка '+app['id'][:8]+'\n'+app['subject']
                    +'\nСтатус: '+('закрыта' if app['status']=='closed' else 'открыта')
                    +'\nУспех: '+('учтён' if app['success'] else 'отменён менеджером' if app['quality']=='invalid' else 'пока не подтверждён')
                    +'\nОснования: '+(', '.join(SUCCESS[c] for c in app['criteria']) or 'нет'))
                labels={'amount':'Сумма','purpose':'Цель','term':'Срок','deadline':'Когда нужно',
                        'banks':'Банки','security':'Обеспечение','contract':'Контракт','guarantee_type':'Вид БГ',
                        'procurement_regime':'Режим закупки','validity':'Срок БГ','document_readiness':'Документы'}
                facts=[labels[k]+': '+v['value'][:120] for k,v in app.get('qualification',{}).items() if k in labels]
                if facts:text+='\nПараметры заявки:\n'+'\n'.join(facts)
                buttons=[]
                if app['quality']=='valid' and app['criteria']:buttons.append(InlineKeyboardButton('Отменить ошибочный успех',callback_data=f'app:i:{app["id"]}:{app["revision"]}'))
                if app['status']=='open':buttons.append(InlineKeyboardButton('Закрыть заявку',callback_data=f'app:c:{app["id"]}:{app["revision"]}'))
                await update.effective_message.reply_text(text,reply_markup=InlineKeyboardMarkup([[b] for b in buttons]))
        if not found:await update.effective_message.reply_text('Подтверждённых заявок пока нет.')
    application.add_handler(CommandHandler('applications',application_cards))
    application.add_handler(CallbackQueryHandler(application_cards,pattern=r'^apps:[0-9a-f]{32}$'))

    async def change_application(update,context):
        from telegram import ForceReply
        query=update.callback_query
        if update.effective_user.id!=account_id or update.effective_chat.type!='private':
            await query.answer('Доступ запрещён.',show_alert=True);return
        match=re.fullmatch(r'app:([ic]):([0-9a-f]{32}):(\d+)',query.data or '')
        if not match:return
        code,app_id,revision=match.groups()
        with store.connection() as db:
            app=db.execute('SELECT a.* FROM seller_applications a JOIN dialogue_threads t ON t.id=a.thread_id '
                'WHERE a.id=? AND t.account_id=? AND t.manager_id=?',(app_id,account_id,manager_id)).fetchone()
        if not app or app['revision']!=int(revision):
            await query.answer('Карточка изменилась. Откройте заявки заново.',show_alert=True);return
        await query.answer()
        prompt=await query.message.reply_text('Укажите причину ответом на это сообщение. До ответа заявка не изменяется.',reply_markup=ForceReply(selective=True))
        context.user_data['application_change']=(app_id,int(revision),'invalidate' if code=='i' else 'close',prompt.message_id)
    application.add_handler(CallbackQueryHandler(change_application,pattern=r'^app:[ic]:'))

    async def application_reason(update,context):
        from dialogue_applications import manager_change
        from telegram.ext import ApplicationHandlerStop
        pending=context.user_data.get('application_change');message=update.effective_message
        if (not pending or update.effective_user.id!=account_id or update.effective_chat.type!='private'
                or not message.reply_to_message or message.reply_to_message.message_id!=pending[3]):return
        try:
            manager_change(store,pending[0],actor=account_id,manager=manager_id,revision=pending[1],action=pending[2],reason=message.text)
            context.user_data.pop('application_change',None)
            await message.reply_text('Изменение сохранено с причиной. История не удалена. ИИ приостановлен; вернуть его можно в карточке диалога.')
        except (ValueError,PermissionError):
            await message.reply_text('Изменение не выполнено. Укажите причину до 1000 символов; если карточка устарела — откройте заявки заново.')
        raise ApplicationHandlerStop
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND,application_reason),group=-6)


async def deliver_notices(bot,store,*,account_id):
    for notice in store.pending_notices(account_id):
        thread=store.thread(notice['thread_id'])
        text=(f"Клиент: {store.client_label(notice['thread_id'])} · {notice['product']}\n"
              f"{notice['summary']}\n\n"
              + ('Автоответы приостановлены.' if notice['state']=='human' else 'Можете подключиться к диалогу.'))
        if thread['seller_enabled']:text=dialogue_card(store,thread,summary=notice['summary'],event_type=notice['event_type'])
        await bot.send_message(chat_id=account_id,text=text,
                               reply_markup=controls(thread['id'],thread['revision'],thread['state'],thread=thread))
        store.acknowledge_notice(notice['id'],account_id=account_id)
