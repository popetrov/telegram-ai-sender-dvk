"""Optional local reporting UI and read receipts; never sends to customer chats."""
import asyncio,json,logging,sqlite3,time,uuid
from pathlib import Path
from service_reports import Scope,initialize,observe_read,reader,writer,rows,build,period,text,excel,stamp
log=logging.getLogger(__name__)

def binding(root,account,manager):
 p=Path(root)/'data/reporting.json'
 if not p.exists():
  import access_runtime
  if access_runtime.account_id!=account or not access_runtime._store:return None
  lease=access_runtime._store.require_access()
  company=lease.get('company_id')
  if not company:return None
  return Scope(company,manager,account,'Организация — в карточке центра',company)
 v=json.loads(p.read_text('utf-8'))
 if v.get('enabled') is not True:return None
 if v.get('format')!=1 or v.get('account_id')!=account or v.get('manager_id')!=manager:raise PermissionError('Отчёты: не совпала привязка менеджера.')
 return Scope(v['company_id'],manager,account,v.get('organization') or 'Организация не подтверждена',v.get('company_name',''),tuple(v.get('allowed_products',[])))

def record_usage(root,*,phase,model,result,status,scope=None,tools=(),product=None,record_id=None):
 """Only the optional reporting recorder; failure never changes an AI turn."""
 try:
  if scope is None:
   import access_runtime,config
   scope=binding(root,access_runtime.account_id,config.MANAGER_ID)
  if scope is None:return
  path=Path(root)/'data/local.sqlite';initialize(path);u=getattr(result,'usage',None)
  data=u.model_dump() if hasattr(u,'model_dump') else u if isinstance(u,dict) else {}
  inp=data.get('input_tokens',data.get('prompt_tokens'));out=data.get('output_tokens',data.get('completion_tokens'));cached=(data.get('input_tokens_details') or {}).get('cached_tokens')
  valid=lambda v:v if type(v) is int and v>=0 else None
  rid=getattr(result,'id',None);key=rid or record_id or uuid.uuid4().hex
  with writer(path) as db:
   db.execute('INSERT OR IGNORE INTO report_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(scope.company_id,scope.manager_id,scope.account_id,key,phase,model,status,valid(inp),valid(out),valid(cached),rid,int(time.time()),json.dumps(dict(usage=data,product=product,tool_calls=[getattr(x,'type','unknown') for x in getattr(result,'output',[]) or [] if getattr(x,'type','') not in {'message','reasoning'}],tools=list(tools)),ensure_ascii=False)))
 except Exception as exc:log.warning('Report usage unavailable (%s)',type(exc).__name__)

async def collect(client,path,scope):
 from telethon import events,functions,types
 from telethon.errors import FloodWaitError
 known=set();receipts_at=None;last_scan=0;error=None;last_beat=0
 def peers():
  with reader(path) as db:return {r['telegram_id'] for r in rows(db,'clients') if r.get('manager_id')==scope.manager_id and (not scope.allowed_products or r.get('product') in scope.allowed_products) and isinstance(r.get('telegram_id'),int) and r['telegram_id']>0}
 known=peers()
 async def on_incoming(event):
  try:
   if not event.is_private or event.out or event.chat_id not in peers():return
   message=event.message
   with writer(path) as db:db.execute('INSERT OR IGNORE INTO report_incoming VALUES(?,?,?,?,?,?,?,?)',
    (scope.company_id,scope.manager_id,scope.account_id,event.chat_id,message.id,int(message.date.timestamp()),message.raw_text or '',int(time.time())))
  except Exception as exc:log.warning('Incoming report unavailable (%s)',type(exc).__name__)
 async def on_read(event):
  try:
   if not event.inbox and event.chat_id in known:observe_read(path,scope,event.chat_id,event.max_id)
  except Exception as exc:log.warning('Read receipt unavailable (%s)',type(exc).__name__)
 client.add_event_handler(on_read,events.MessageRead(inbox=False))
 client.add_event_handler(on_incoming,events.NewMessage(incoming=True))
 try:
  while True:
   connected=bool(client.is_connected())
   try:
    import access_runtime
    if access_runtime._store and access_runtime._store.require_access().get('reporting_schema')==1:
     from sync_data import enable_report_export
     with writer(path) as db:
      db.row_factory=sqlite3.Row;enable_report_export(db)
   except Exception as exc:log.debug('Report export deferred (%s)',type(exc).__name__)
   with writer(path) as db:db.execute('INSERT OR REPLACE INTO report_health VALUES(?,?,?,?,?,?)',(scope.company_id,scope.account_id,int(time.time()),int(connected),receipts_at,error))
   if connected and time.monotonic()-last_scan>=900:
    known=peers();error=None;ok=True
    for peer in sorted(known):
     try:
      entity=await client.get_input_entity(peer)
      if not isinstance(entity,types.InputPeerUser):continue
      result=await client(functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(entity)]),flood_sleep_threshold=0)
      if result.dialogs:observe_read(path,scope,peer,result.dialogs[0].read_outbox_max_id)
      else:ok=False
     except FloodWaitError as exc:
      error='Telegram: пауза '+str(exc.seconds)+' с';ok=False
      # No new requests until Telegram's server wait is over. Heartbeats remain active.
      last_scan=time.monotonic()+exc.seconds-900;break
     except (ValueError,TypeError):ok=False
     except Exception as exc:error=type(exc).__name__;ok=False
     await asyncio.sleep(.3)
     if time.monotonic()-last_beat>=20:
      with writer(path) as db:db.execute('INSERT OR REPLACE INTO report_health VALUES(?,?,?,?,?,?)',(scope.company_id,scope.account_id,int(time.time()),1,receipts_at,error))
      last_beat=time.monotonic()
    if ok:receipts_at=int(time.time())
    if not error or not error.startswith('Telegram: пауза'):last_scan=time.monotonic()
   await asyncio.sleep(15)
 finally:
  client.remove_event_handler(on_read)
  client.remove_event_handler(on_incoming)
  with writer(path) as db:db.execute('INSERT OR REPLACE INTO report_health VALUES(?,?,?,?,?,?)',(scope.company_id,scope.account_id,int(time.time()),0,receipts_at,error))

async def start(client,root):
 import access_runtime,config
 async def run():
  while True:
   try:scope=binding(root,access_runtime.account_id,config.MANAGER_ID)
   except Exception:scope=None
   if scope is not None:break
   await asyncio.sleep(10)
  me=await client.get_me();scope.authorize(me.id)
  path=Path(root)/'data/local.sqlite';initialize(path)
  await collect(client,path,scope)
 task=asyncio.create_task(run(),name='reporting-read-receipts')
 def finished(task):
  if not task.cancelled() and task.exception() is not None:log.warning('Reporting collector stopped (%s)',type(task.exception()).__name__)
 task.add_done_callback(finished);return task

def register(application,*,root,allowed):
 from telegram import InlineKeyboardButton as B,InlineKeyboardMarkup as K
 from telegram.ext import CommandHandler,CallbackQueryHandler,MessageHandler,filters,ApplicationHandlerStop
 import access_runtime,config
 root=Path(root);path=root/'data/local.sqlite';key='service_report_state'
 def authorize(update):
  if update.effective_chat.type!='private' or not allowed(update):raise PermissionError('Нет доступа.')
  s=binding(root,access_runtime.account_id,config.MANAGER_ID)
  if s is None:raise PermissionError('Отчёты в этой установке ещё не включены.')
  s.authorize(update.effective_user.id);return s
 def buttons(token,scope):
  rows=[[B('Показать заявки',callback_data='rpt:'+token+':apps:0'),B('Выгрузить Excel',callback_data='rpt:'+token+':excel')],[B('Другой период',callback_data='rpt:'+token+':period')]]
  if scope.allowed_products:rows.append([B('Все продукты',callback_data='rpt:'+token+':prod:0')]+[B(p,callback_data='rpt:'+token+':prod:'+str(i+1)) for i,p in enumerate(scope.allowed_products)])
  return K(rows)
 async def choose(update,context):
  try:authorize(update)
  except PermissionError as exc:await update.effective_message.reply_text(str(exc));return
  token=uuid.uuid4().hex[:12];context.user_data[key]=dict(token=token,custom=False,created_at=time.time())
  await update.effective_message.reply_text('За какой период получить отчёт? Время — Дубай.',reply_markup=K([[B('Сегодня',callback_data='rpt:'+token+':p:today'),B('Вчера',callback_data='rpt:'+token+':p:yesterday')],[B('7 дней',callback_data='rpt:'+token+':p:7'),B('30 дней',callback_data='rpt:'+token+':p:30')],[B('Всё время',callback_data='rpt:'+token+':p:all'),B('Указать даты',callback_data='rpt:'+token+':custom')]]))
 async def generate(update,context,scope,value):
  start,end=period(value);state=context.user_data[key];state.update(start=start,end=end,custom=False)
  report=await asyncio.to_thread(build,path,scope,actor=update.effective_user.id,start=start,end=end,product=state.get('product'))
  await update.effective_message.reply_text(text(report),reply_markup=buttons(state['token'],scope))
 async def callback(update,context):
  q=update.callback_query;await q.answer()
  try:
   scope=authorize(update);state=context.user_data.get(key);parts=q.data.split(':')
   if not state or parts[1]!=state['token'] or time.time()-state['created_at']>3600:raise ValueError('Отчёт устарел. Нажмите «Получить отчёт» снова.')
   op=parts[2]
   if op=='period':await choose(update,context);return
   if op=='custom':state['custom']=True;await update.effective_message.reply_text('Введите даты включительно: 01.10.2026–31.10.2026. /cancel_report отменяет ввод.');return
   if op=='p':await generate(update,context,scope,parts[3]);return
   if 'start' not in state:raise ValueError('Сначала выберите период.')
   if op=='prod':
    i=int(parts[3])
    if not 0<=i<=len(scope.allowed_products):raise ValueError('Продукт недоступен.')
    state['product']=scope.allowed_products[i-1] if i else None
   report=await asyncio.to_thread(build,path,scope,actor=update.effective_user.id,start=state['start'],end=state['end'],product=state.get('product'))
   if op=='prod':await update.effective_message.reply_text(text(report),reply_markup=buttons(state['token'],scope));return
   if op=='excel':
    # Generated from one consistent report snapshot, never a client-supplied path.
    stream=await asyncio.to_thread(excel,report)
    await update.effective_message.reply_document(document=stream,filename='Отчёт_'+str(state['start'])+'.xlsx',caption='Отчёт и основания. Время — Дубай.');return
   if op=='apps':
    page=max(0,int(parts[3]));apps=report['applications'];chunk=apps[page*8:(page+1)*8]
    if not chunk:await update.effective_message.reply_text('За выбранный период заявок нет.');return
    keys=[[B((a['name'] or str(a['peer']))[:30]+' · '+a['product'],callback_data='rpt:'+state['token']+':app:'+a['id'])] for a in chunk]
    if (page+1)*8<len(apps):keys.append([B('Далее',callback_data='rpt:'+state['token']+':apps:'+str(page+1))])
    await update.effective_message.reply_text('Заявки за период: '+str(len(apps)),reply_markup=K(keys));return
   if op=='app':
    a=next((a for a in report['applications'] if a['id']==parts[3]),None)
    if a is None:raise PermissionError('Заявка недоступна в этом отчёте.')
    t='\n'.join(['Клиент: '+(a['name'] or str(a['peer'])),'Менеджер: '+a['manager'],'Продукт: '+a['product'],'Потребность: '+a['subject'],'Передача МОП: '+('уведомление доставлено' if any(n['status']=='delivered' for n in a['transfers']) else 'уведомление ожидает доставки' if a['transfers'] else 'не зафиксирована'),'Создана: '+stamp(a['created_at']),'Статус: '+a['status']+'; качество: '+a['quality'],'Основание — сообщение клиента:',a['need_text'][:1800],'Уточнения: '+str(a.get('qualification','{}'))[:700]])
    await update.effective_message.reply_text(t[:3900],reply_markup=K([[B('Открыть сообщение клиента',url=a['url'])]]));return
   raise ValueError('Неизвестное действие отчёта.')
  except (ValueError,PermissionError) as exc:await update.effective_message.reply_text(str(exc))
  except Exception as exc:
   log.warning('Report unavailable (%s)',type(exc).__name__);await update.effective_message.reply_text('Не удалось сформировать отчёт. Клиентские отправки не затронуты. Повторите /report.')
  finally:raise ApplicationHandlerStop
 async def custom(update,context):
  state=context.user_data.get(key)
  if not state or not state.get('custom'):return
  from bot_menu import SENDER
  if update.effective_message.text in SENDER:state['custom']=False;return
  try:
   scope=authorize(update)
   if time.time()-state['created_at']>3600:state['custom']=False;raise ValueError('Ввод устарел. Повторите /report.')
   await generate(update,context,scope,update.effective_message.text)
  except (ValueError,PermissionError) as exc:await update.effective_message.reply_text(str(exc))
  finally:raise ApplicationHandlerStop
 async def cancel(update,context):
  authorize(update);context.user_data.pop(key,None);await update.effective_message.reply_text('Ввод периода отменён.');raise ApplicationHandlerStop
 application.add_handler(CommandHandler(['report','statistics'],choose),group=-7)
 application.add_handler(CommandHandler('cancel_report',cancel),group=-7)
 application.add_handler(CallbackQueryHandler(callback,pattern=r'^rpt:'),group=-7)
 application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,custom),group=-6)
