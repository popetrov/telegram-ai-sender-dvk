from finance_scope import account_allowed, thread_allowed, current_account, current_manager
"""Owner's calendar controls in the existing bot. Never directly sends to clients."""
import re
from dialogue_calendar import (GROUPS,KINDS,initialize,exists,bound,had_application_handoff,assign,
 owner_task,task_change,list_tasks,label_time)
from dialogue_store import StaleDialogue
from dialogue_seller import OWNER_ACCOUNT_ID

STATUS={'proposed':'План: ждёт разрешения на отправки','scheduled':'Запланировано','preparing':'Подготавливается','queued':'В очереди отправки','sent':'Отправлено','cancelled':'Отменено','blocked':'Нужна проверка','done':'Выполнено'}
CODES={'p':'post_application','c':'conversation','s':'silent'}


def live_enabled(store):
 with store.connection() as db:
  if not exists(db):return False
  r=db.execute("SELECT value FROM seller_calendar_meta WHERE key='live_enabled'").fetchone()
  test=db.execute("SELECT value FROM seller_calendar_meta WHERE key='test_peer_id'").fetchone()
  return bool(r and r[0]=='true' and not (test and test[0]))


def status_line(store,thread):
 with store.connection() as db:
  if not exists(db):return ''
  c=db.execute('SELECT * FROM seller_warm_cohorts WHERE thread_id=?',(thread['id'],)).fetchone()
  f=db.execute("SELECT * FROM seller_followups WHERE thread_id=? AND status IN ('proposed','scheduled','preparing','queued','blocked') ORDER BY due_at LIMIT 1",(thread['id'],)).fetchone()
 if not c and not f:return ''
 text='Календарь'
 if c:text+=': '+GROUPS[c['category']]+(' — на паузе' if c['status'] in {'paused','stopped'} else '')
 if f:text+='\n'+KINDS[f['kind']]+': '+(label_time(f['due_at']) if f['due_at'] else 'дата уточняется')+'\n'+STATUS[f['status']]
 return text


async def show_groups(message,store,tid,actor,manager):
 from telegram import InlineKeyboardButton as B,InlineKeyboardMarkup as M
 t=bound(store,tid,actor,manager)
 _,history=store.snapshot(tid)
 incoming=any(m['direction']=='incoming' and m['sent_at']>=t['first_sent_at'] for m in history)
 app=had_application_handoff(store,tid);choices=[]
 if app:choices.append(('p','Вернуть после заявки → прогрев 2+1'))
 elif incoming:choices.append(('c','Общались без заявки → раз в месяц'))
 else:choices.append(('s','Молчащий → раз в 3 месяца'))
 text=('Выберите сценарий для '+store.client_label(tid)+'.\n'
  +'После заявки у МОП: до 2 социальных и 1 продуктового касания в месяц, только по явному возврату.\n'
  +'Без заявки: одно сообщение в месяц. Молчащим: раз в 3 месяца; после двух безответных одно последнее социальное и стоп.\n'
  +'«Вернуть ИИ» продолжает переговоры и не включает прогрев.')
 if not live_enabled(store):text+='\n\nСейчас создаётся только план. Плановые отправки ещё не разрешены.'
 await message.reply_text(text,reply_markup=M([[B(label,callback_data=f'cal:g:{tid}:{t["revision"]}:{code}')] for code,label in choices]))


def register(application,store,*,account_id,manager_id):
 from telegram import InlineKeyboardButton as B,InlineKeyboardMarkup as M,ForceReply
 from telegram.ext import CallbackQueryHandler,CommandHandler,MessageHandler,filters,ApplicationHandlerStop
 def allowed(update):return update.effective_user.id==account_id and account_allowed(account_id) and update.effective_chat.type=='private'
 def access_tid(tid):
  t=store.thread(tid)
  if t['account_id']!=account_id or t['manager_id']!=manager_id or t['company_id']!='dvk':raise PermissionError('Доступ запрещён')
  return t
 async def view_task(message,id):
  with store.connection() as db:r=db.execute('SELECT * FROM seller_followups WHERE id=?',(id,)).fetchone()
  if not r:raise ValueError('Задача не найдена')
  t=access_tid(r['thread_id'])
  text=(store.client_label(t['id'])+'\n'+KINDS[r['kind']]+' · '+STATUS[r['status']]
   +'\nКогда: '+(label_time(r['due_at']) if r['due_at'] else 'уточняется')+'\nЦель: '+r['purpose'])
  if r['source_quote']:text+='\nСлова клиента: '+r['source_quote'][:350]
  if r['reason']:text+='\n'+r['reason']
  if r['kind'].startswith('manager_'):text+='\nЗадача для МОП. Звонок или письмо нужно выполнить самостоятельно.'
  if t['mode'] in {'off','copilot','paused_until'} or t['state']=='human':text+='\nДиалог у МОП или на паузе. Задача не возвращает его ИИ.'
  rows=[]
  if r['status'] in {'proposed','scheduled','preparing','queued','blocked'}:
   rows.append([B('Перенести',callback_data=f'cal:m:{r["id"]}:{r["revision"]}'),B('Отменить',callback_data=f'cal:x:{r["id"]}:{r["revision"]}')])
   rows.append([B('Отметить выполненной',callback_data=f'cal:d:{r["id"]}:{r["revision"]}')])
  await message.reply_text(text,reply_markup=M(rows) if rows else None)
 async def calendar(update,context):
  if not allowed(update):return
  initialize(store)
  args=context.args or []
  try:page=max(0,int(args[0])-1) if args else 0
  except ValueError:page=0
  tasks=list_tasks(store,account_id,manager_id,limit=11,offset=page*10)
  head='Календарь ДВК Финанс · время МСК\n'
  head+=('Плановые отправки разрешены.' if live_enabled(store) else 'Плановые отправки выключены. Здесь можно подготовить и проверить задачи.')
  if not tasks:head+='\nЗадач пока нет. Откройте «Диалоги» → «Календарь и прогрев».'
  await update.effective_message.reply_text(head)
  for f in tasks[:10]:
   await update.effective_message.reply_text(store.client_label(f['thread_id'])+' · '+KINDS[f['kind']]+'\n'+(label_time(f['due_at']) if f['due_at'] else 'Дата уточняется')+'\n'+STATUS[f['status']],reply_markup=M([[B('Открыть задачу',callback_data='cal:f:'+f['id'])]]))
  nav=[]
  if page:nav.append(B('← Назад',callback_data='cal:page:'+str(page)))
  if len(tasks)>10:nav.append(B('Дальше →',callback_data='cal:page:'+str(page+2)))
  if nav:await update.effective_message.reply_text('Страница '+str(page+1),reply_markup=M([nav]))
 application.add_handler(CommandHandler(['calendar','warmup'],calendar))
 async def callback(update,context):
  q=update.callback_query
  if not allowed(update):await q.answer('Доступ запрещён',show_alert=True);return
  p=(q.data or '').split(':')
  try:
   initialize(store);op=p[1]
   if op=='page':
    await q.answer();context.args=[p[2]];await calendar(update,context);return
   if op=='t':
    t=access_tid(p[2]);await q.answer()
    text=store.client_label(t['id'])+'\n'+(status_line(store,t) or 'Задач и группы прогрева пока нет.')
    rows=[[B('Создать задачу',callback_data='cal:new:'+t['id'])],[B('Выбрать группу прогрева',callback_data='cal:groups:'+t['id'])]]
    with store.connection() as db:jobs=db.execute("SELECT id FROM seller_followups WHERE thread_id=? AND status IN ('proposed','scheduled','preparing','queued','blocked') ORDER BY due_at LIMIT 8",(t['id'],)).fetchall()
    rows+=[[B('Открыть задачу '+str(i+1),callback_data='cal:f:'+r['id'])] for i,r in enumerate(jobs)]
    await q.message.reply_text(text,reply_markup=M(rows));return
   if op=='groups':
    await q.answer();await show_groups(q.message,store,p[2],account_id,manager_id);return
   if op=='g':
    tid,revision,code=p[2:];access_tid(tid)
    id=assign(store,tid,CODES[code],actor=account_id,manager=manager_id,revision=int(revision),live=live_enabled(store))
    await q.answer('Группа сохранена.');await view_task(q.message,id);return
   if op=='new':
    t=access_tid(p[2]);await q.answer()
    await q.message.reply_text('Что нужно сделать?',reply_markup=M([[B('Написать в этот чат',callback_data=f'cal:n:{t["id"]}:t')],[B('Звонок МОП',callback_data=f'cal:n:{t["id"]}:c'),B('Письмо МОП',callback_data=f'cal:n:{t["id"]}:e')]]));return
   if op=='n':
    t=access_tid(p[2]);kind={'t':'agreed','c':'manager_call','e':'manager_email'}[p[3]];await q.answer()
    prompt=await q.message.reply_text('Когда и с какой целью? Ответьте на это сообщение.\nНапример: «завтра в 11 | обсудить новую гарантию» или «30.10.2026 14:00 | звонок по заявке».\nПо умолчанию время МСК. Плановые сообщения — в рабочие дни с 09:00 до 18:00 МСК.',reply_markup=ForceReply(selective=True))
    context.user_data['calendar_input']=('new',t['id'],t['revision'],kind,prompt.message_id);return
   if op=='f':
    await q.answer();await view_task(q.message,p[2]);return
   if op in {'m','x','d'}:
    id,revision=p[2:];revision=int(revision)
    with store.connection() as db:r=db.execute('SELECT * FROM seller_followups WHERE id=?',(id,)).fetchone()
    if not r:raise ValueError('Задача не найдена')
    access_tid(r['thread_id'])
    if op=='m':
     await q.answer();prompt=await q.message.reply_text('На какую дату перенести? Например: «завтра в 11» или «30.10.2026 14:00». Время МСК.',reply_markup=ForceReply(selective=True));context.user_data['calendar_input']=('move',id,revision,None,prompt.message_id);return
    task_change(store,id,actor=account_id,manager=manager_id,revision=revision,action='cancel' if op=='x' else 'done');await q.answer('Сохранено.');await q.message.reply_text('Задача отменена.' if op=='x' else 'Задача отмечена выполненной.');return
  except (ValueError,KeyError,PermissionError,StaleDialogue) as e:
   await q.answer(str(e)[:180] or 'Карточка изменилась. Откройте её заново.',show_alert=True)
 application.add_handler(CallbackQueryHandler(callback,pattern=r'^cal:'))
 async def reply(update,context):
  pending=context.user_data.get('calendar_input');m=update.effective_message
  if not pending or not allowed(update) or not m.reply_to_message or m.reply_to_message.message_id!=pending[4]:return
  try:
   op,id,revision,kind,_=pending
   if op=='move':task_change(store,id,actor=account_id,manager=manager_id,revision=revision,action='move',when=m.text)
   else:
    parts=(m.text or '').split('|',1)
    if len(parts)!=2:raise ValueError('Разделите дату и цель знаком |')
    id=owner_task(store,id,actor=account_id,manager=manager_id,revision=revision,when=parts[0],purpose=parts[1],kind=kind,live=live_enabled(store))
   context.user_data.pop('calendar_input',None);await view_task(m,id)
  except (ValueError,PermissionError,StaleDialogue) as e:await m.reply_text(str(e)+'\nЗадача не изменена; можно исправить ответ.')
  raise ApplicationHandlerStop
 application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND,reply),group=-8)
