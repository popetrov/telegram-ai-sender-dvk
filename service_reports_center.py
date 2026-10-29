"""Center adapter contract, not activated by a manager package.
The center resolves membership from its own roster, never from submitted rows.
Each materialized manager DB is separately scoped; tenant path comes from server code.
"""
from pathlib import Path
from service_reports import Scope,build

# Logical record IDs are scoped by account and installation, never global.
KEYS={'clients':('id',),'messages':('id',),'research_runs':('id',),
 'dialogue_threads':('id',),'dialogue_messages':('thread_id','telegram_id'),
 'dialogue_notices':('id',),'seller_applications':('id',),
 'seller_application_events':('id',),'seller_warm_cohorts':('thread_id',),
 'seller_ai_usage':('id',),'report_usage':('id',),
 'report_health':('company_id','account_id'),'report_reads':('company_id','account_id','peer_id'),
 'report_incoming':('company_id','account_id','peer_id','message_id')}

def center_report(authority,actor,admin_id,*,start,end,company_id=None,organization=None,company_name=None,account_id=None):
 """Rebuild read-only views from acknowledged events, bound to central roster."""
 import json,sqlite3,tempfile,time
 from contextlib import closing
 from service_reports import initialize
 if type(actor) is not int or actor!=admin_id:raise PermissionError('Только администратор центра.')
 with authority.connection() as db:
  members=[dict(r) for r in db.execute('''SELECT m.account_id,m.subject,m.company_id,
   COALESCE(d.full_name,'') AS full_name,COALESCE(d.department,'Не указана') AS department,
   COALESCE(d.company_name,m.company_id) AS company_name FROM managers m
   LEFT JOIN manager_directory d ON d.account_id=m.account_id WHERE m.company_id IS NOT NULL''')]
  members=[m for m in members if (company_id is None or m['company_id']==company_id) and
   (organization is None or m['department']==organization) and (company_name is None or m['company_name']==company_name)
   and (account_id is None or m['account_id']==account_id)]
  if account_id is not None and not members:raise PermissionError('Менеджер не найден в выбранной группе.')
  source={m['account_id']:[dict(r) for r in db.execute('SELECT rowid AS sequence,payload,received_at FROM events WHERE account_id=? AND company_id=? AND subject=? ORDER BY rowid',
   (m['account_id'],m['company_id'],m['subject']))] for m in members}
 reports=[];missing=[]
 for m in members:
  events=source[m['account_id']];data={};latest_installation=None
  for e in events:
   p=json.loads(e['payload'])
   if p.get('data',{}).get('table') in KEYS:latest_installation=p.get('installation')
  for e in events:
   p=json.loads(e['payload']);item=p.get('data',{});table=item.get('table');row=item.get('row')
   if table not in KEYS or not isinstance(row,dict) or p.get('installation')!=latest_installation:continue
   if any(k not in row for k in KEYS[table]):continue
   key=tuple(row[k] for k in KEYS[table]);data.setdefault(table,{})[key]=row
  if not data.get('clients'):
   missing.append(m['account_id']);continue
  with tempfile.TemporaryDirectory(prefix='dvk-report-') as tmp:
   path=Path(tmp)/'projection.sqlite'
   # Names are validated and values bound. No received SQL is executed.
   import re
   with closing(sqlite3.connect(path)) as projection:
    for table,records in data.items():
     columns=sorted({c for row in records.values() for c in row if re.fullmatch(r'[a-z_][a-z0-9_]*',c)})
     if not columns:continue
     projection.execute('CREATE TABLE "'+table+'" ('+','.join('"'+c+'"' for c in columns)+')')
     for row in records.values():projection.execute('INSERT INTO "'+table+'" VALUES('+','.join('?' for c in columns)+')',
      [json.dumps(row.get(c)) if isinstance(row.get(c),(dict,list)) else row.get(c) for c in columns])
    projection.commit()
   initialize(path)
   manager=m['subject'].rsplit(':',1)[0]
   report=build(path,Scope(m['company_id'],manager,m['account_id'],m['department'],m['company_name']),actor=m['account_id'],start=start,end=end)
   report['member']=m;report['last_received']=max(e['received_at'] for e in events)
   report['reporting_available']=bool(data.get('report_health'))
   reports.append(report)
 totals={};costs=dict(usd_min=0.,usd_max=0.,tool_usd_known=0.,unknown_requests=0,incomplete_tools=0,priced_requests=0)
 for r in reports:
  for key,value in r['metrics'].items():totals[key]=totals.get(key,0)+value
  for key in costs:costs[key]+=r['costs'].get(key,0)
 return dict(metrics=totals,costs=costs,managers=reports,missing_accounts=missing,total_managers=len(members),
  reporting_managers=sum(r['reporting_available'] for r in reports),start=start,end=end,complete=False)

def register(application,authority,admin_id):
 import asyncio,io,time,uuid
 from telegram import InlineKeyboardButton as B,InlineKeyboardMarkup as K
 from telegram.ext import CommandHandler,CallbackQueryHandler,ApplicationHandlerStop
 from service_reports import period,stamp,LABELS
 state_key='center_statistics'
 def authorize(update):
  if update.effective_user.id!=admin_id or update.effective_chat.type!='private':raise PermissionError('Нет доступа.')
 def keyboard(token,items):return K([[B(label[:60],callback_data='cs:'+token+':'+value)] for label,value in items])
 async def choose(update,context):
  authorize(update);token=uuid.uuid4().hex[:10]
  context.user_data[state_key]=dict(token=token,created_at=time.time(),filters={})
  await update.effective_message.reply_text('Статистика центра. Выберите охват:',reply_markup=keyboard(token,[('Все менеджеры','all'),('По организации','organizations'),('По компании','companies'),('По менеджеру','members')]))
 async def callback(update,context):
  try:
   authorize(update);q=update.callback_query;await q.answer();state=context.user_data.get(state_key);parts=q.data.split(':')
   if not state or parts[1]!=state['token'] or time.time()-state['created_at']>3600:raise ValueError('Кнопки устарели. Нажмите «Статистика».')
   op=parts[2];token=state['token']
   if op in ('organizations','companies','members'):
    with authority.connection() as db:
     rows=[dict(r) for r in db.execute('''SELECT m.account_id,m.company_id,COALESCE(d.full_name,'') AS full_name,
      COALESCE(d.department,'Не указана') AS department,COALESCE(d.company_name,m.company_id) AS company_name FROM managers m
      LEFT JOIN manager_directory d ON d.account_id=m.account_id WHERE m.company_id IS NOT NULL ORDER BY d.full_name,m.account_id''')]
    if op=='organizations':options=[(o,dict(organization=o)) for o in sorted({r['department'] for r in rows})]
    elif op=='companies':options=[(c,dict(company_name=c)) for c in sorted({r['company_name'] for r in rows})]
    else:options=[((r['full_name'] or 'Менеджер')+' · '+str(r['account_id']),dict(account_id=r['account_id'])) for r in rows]
    state['options']=options
    # Split messages to keep Telegram keyboards bounded.
    for offset in range(0,len(options),40):await update.effective_message.reply_text('Выберите:',reply_markup=keyboard(token,[(label,'select:'+str(i)) for i,(label,_) in enumerate(options) if offset<=i<offset+40]))
    if not options:await update.effective_message.reply_text('Подключённых менеджеров в карточках нет.')
    return
   if op=='select':
    index=int(parts[3]);options=state.get('options',[])
    if not 0<=index<len(options):raise ValueError('Недоступный выбор')
    state['label'],state['filters']=options[index]
   if op in ('all','select'):
    if op=='all':state.update(label='Все менеджеры',filters={})
    await update.effective_message.reply_text('За какой период?',reply_markup=keyboard(token,[('Сегодня','p:today'),('Вчера','p:yesterday'),('7 дней','p:7'),('30 дней','p:30'),('Всё время','p:all')]))
    return
   if op=='p':
    start,end=period(parts[3]);state.update(start=start,end=end)
    report=await asyncio.to_thread(center_report,authority,admin_id,admin_id,start=start,end=end,**state['filters'])
    state['report']=report
    lines=['Статистика центра: '+state['label'],stamp(start)+' — '+stamp(end-1)+' (Дубай)',
     'Менеджеров в охвате: '+str(report['total_managers']),
     'С полученными данными: '+str(len(report['managers'])),
     'С новым сборщиком статистики: '+str(report['reporting_managers'])]
    lines += [LABELS[k]+': '+str(v) for k,v in report['metrics'].items()]
    costs=report['costs'];lines+=['',('Учтённые токены выбранных менеджеров: $%.4f–$%.4f'%(costs['usd_min'],costs['usd_max']) if costs['priced_requests'] else 'Расходы ИИ: пока недостаточно полученных записей для расчёта, это НЕ нулевой расход'),
     'Учтённые вызовы инструментов: $%.4f'%costs['tool_usd_known'],
     'Запросов без расчёта: '+str(costs['unknown_requests']),
     'Это НЕ общие списания ключа. История до включения учёта и внешние запросы могут отсутствовать.',
     'Отсутствующие данные не означают ноль. Сводка по полученным записям; синхронизация может запаздывать.']
    if report['missing_accounts']:lines.append('Нет данных: '+', '.join(map(str,report['missing_accounts'])))
    await update.effective_message.reply_text('\n'.join(lines)[:4000],reply_markup=keyboard(token,[('Скачать сводку CSV','csv'),('Списания OpenAI — вся организация','billing')]))
    return
   if op=='csv':
    import csv
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['Менеджер','ID','Компания','Организация','Отправлено','Входящих','Учтено токены USD минимум','Учтено токены USD максимум','Последняя синхронизация'])
    for r in state['report']['managers']:
     m=r['member'];name=m['full_name']
     if name.lstrip().startswith(('=','+','-','@')):name="'"+name
     values=[name,m['account_id'],m['company_name'],m['department'],r['metrics']['sent_messages'],r['metrics']['reply_messages'],r['costs']['usd_min'],r['costs']['usd_max'],stamp(r['last_received'])]
     writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
    await update.effective_message.reply_document(document=io.BytesIO(stream.getvalue().encode('utf-8-sig')),filename='statistics.csv');return
   if op=='billing':
    from ai_costs import provider_costs
    result=await asyncio.to_thread(provider_costs,state['start'],state['end'])
    if not result['available']:
     await update.effective_message.reply_text('Фактические списания пока не подключены: центру нужен отдельный OpenAI Admin API key. Обычный ключ менеджеров не подходит. Ключ вводится локально на компьютере владельца и менеджерам не передаётся. Местная оценка не заменяет списания в OpenAI.');return
    from datetime import datetime,timezone
    utc=lambda v:datetime.fromtimestamp(v,timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    await update.effective_message.reply_text('OpenAI: $%.4f за %s — %s. Это ВСЯ организация OpenAI, все её проекты и ключи, а не выбранный менеджер. Данные провайдера могут поступать с задержкой. Границы суток — UTC; они не совпадают с отчётом по Дубаю.'%(result['usd'],utc(result['start']),utc(result['end'])))
    return
   raise ValueError('Неизвестная кнопка статистики.')
  except (ValueError,PermissionError) as exc:await update.effective_message.reply_text(str(exc))
  except Exception:
   import logging
   logging.getLogger(__name__).exception('Statistics unavailable')
   await update.effective_message.reply_text('Статистика временно недоступна. Данные и отправки не изменены. Повторите /statistics.')
  finally:raise ApplicationHandlerStop
 application.add_handler(CommandHandler(['statistics','report'],choose),group=-7)
 application.add_handler(CallbackQueryHandler(callback,pattern=r'^cs:'),group=-7)

class CenterReports:
 def __init__(self,authority,*,admin_ids,resolve_projection):
  self.authority=authority;self.admin_ids=frozenset(admin_ids);self.resolve_projection=resolve_projection
 def authorize(self,actor):
  if type(actor) is not int or actor not in self.admin_ids:raise PermissionError('Отчёты центра доступны администратору.')
 def organizations(self,actor):
  self.authorize(actor)
  with self.authority.connection() as db:
   return [dict(r) for r in db.execute('''SELECT DISTINCT m.company_id,d.department,d.company_name FROM managers m
    JOIN manager_directory d ON d.account_id=m.account_id WHERE m.company_id IS NOT NULL
    ORDER BY d.department,d.company_name,m.company_id''')]
 def members(self,actor,*,company_id,organization,company_name):
  self.authorize(actor)
  with self.authority.connection() as db:
   return [dict(r) for r in db.execute('''SELECT m.account_id,m.subject,m.company_id,d.department,d.company_name,d.delivered_revision,d.revision
    FROM managers m JOIN manager_directory d ON d.account_id=m.account_id
    WHERE m.company_id=? AND d.department=? AND d.company_name=? ORDER BY m.account_id''',(company_id,organization,company_name))]
 def report(self,actor,*,company_id,organization,company_name,start,end,account_id=None):
  members=self.members(actor,company_id=company_id,organization=organization,company_name=company_name)
  if account_id is not None:
   members=[m for m in members if m['account_id']==account_id]
   if not members:raise PermissionError('Менеджер не относится к выбранной организации.')
  if not members:raise ValueError('Нет подтверждённых карточек в выбранной организации.')
  results=[];missing=[]
  for m in members:
   manager=m['subject'].rsplit(':',1)[0];scope=Scope(m['company_id'],manager,m['account_id'],m['department'],m['company_name'])
   # Resolver belongs to the authenticated projector, never to callback parameters.
   p=self.resolve_projection(m['company_id'],m['account_id'])
   if p is None:missing.append(m['account_id']);continue
   results.append(build(p,scope,actor=m['account_id'],start=start,end=end))
  totals={}
  for r in results:
   for k,v in r['metrics'].items():totals[k]=totals.get(k,0)+v
  return dict(organization=organization,company_name=company_name,company_id=company_id,metrics=totals,managers=results,missing_accounts=missing,complete=not missing)
