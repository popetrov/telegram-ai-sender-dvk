from finance_scope import account_allowed, thread_allowed, current_account, current_manager
"""Private DVK stage 3: explicit cohorts, sourced calendar tasks and gated delivery."""
import asyncio,calendar,json,re,time,uuid,hashlib
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from dialogue_store import StaleDialogue
from dialogue_seller import OWNER_ACCOUNT_ID,can_send

MSK=ZoneInfo('Europe/Moscow')
# Official holiday list checked 2026-10-28: https://www.cbr.ru/other/holidays/
FIXED_HOLIDAYS={(1,d) for d in range(1,9)}|{(2,23),(3,8),(5,1),(5,9),(6,12),(11,4)}
EXTRA_CLOSED={'2026-01-09','2026-03-09','2026-05-11','2026-12-31','2027-02-22','2027-05-03','2027-05-10','2027-06-14','2027-11-05','2027-12-31'}
def business_day(d):return d.weekday()<5 and (d.month,d.day) not in FIXED_HOLIDAYS and d.date().isoformat() not in EXTRA_CLOSED

GROUPS={'post_application':'После заявки у МОП','conversation':'Общались без заявки','silent':'Молчащие'}
KINDS={'social':'Поддержать контакт','product':'Продуктовое','reconnect':'Уточнить потребность','final_social':'Последнее социальное','agreed':'По договорённости','manager_call':'Звонок МОП','manager_email':'Письмо МОП','manager_message':'Сообщение МОП'}
OPEN=('proposed','scheduled','preparing','queued')


def initialize(store):
 ensure_audit_table(store)
 with store.connection() as db:
  db.executescript('''CREATE TABLE IF NOT EXISTS seller_warm_cohorts (
   thread_id TEXT PRIMARY KEY,category TEXT NOT NULL,status TEXT NOT NULL,
   revision INTEGER NOT NULL DEFAULT 1,returned_at INTEGER,last_touch_at INTEGER,
   cycle INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS seller_followups (
   id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,category TEXT NOT NULL,kind TEXT NOT NULL,
   due_at INTEGER NOT NULL,purpose TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
   source_message_id INTEGER,source_quote TEXT NOT NULL DEFAULT '',proof TEXT NOT NULL DEFAULT '{}',
   action_id TEXT UNIQUE,draft TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
   created_by TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
   UNIQUE(thread_id,category,source_message_id));
  CREATE INDEX IF NOT EXISTS seller_followups_due ON seller_followups(status,due_at);
  CREATE TABLE IF NOT EXISTS seller_calendar_events (
   id TEXT PRIMARY KEY,task_id TEXT,thread_id TEXT NOT NULL,event TEXT NOT NULL,
   data TEXT NOT NULL,created_at INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS seller_calendar_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')


def exists(db):
 return db.execute("SELECT 1 FROM sqlite_master WHERE name='seller_followups'").fetchone() is not None


def audit(db,tid,event,now,task_id=None,**data):
 db.execute('INSERT INTO seller_calendar_events VALUES(?,?,?,?,?,?)',
  (uuid.uuid4().hex,task_id,tid,event,json.dumps(data,ensure_ascii=False,sort_keys=True),int(now)))


def month_add(ts,months):
 d=datetime.fromtimestamp(ts,MSK);m=d.year*12+d.month-1+months;y,m=divmod(m,12)
 return int(d.replace(year=y,month=m+1,day=min(d.day,calendar.monthrange(y,m+1)[1])).timestamp())


def working(ts):
 d=datetime.fromtimestamp(ts,MSK)
 return business_day(d) and 9<=d.hour<18


def business_at(ts,tid='',*,day_only=False):
 d=datetime.fromtimestamp(ts,MSK)
 if day_only:d=d.replace(hour=10,minute=int(hashlib.sha256(tid.encode()).hexdigest()[:4],16)%45,second=0,microsecond=0)
 if d.hour<9:d=d.replace(hour=9,minute=0,second=0,microsecond=0)
 elif d.hour>=18:d=(d+timedelta(days=1)).replace(hour=9,minute=0,second=0,microsecond=0)
 while not business_day(d):d=(d+timedelta(days=1)).replace(hour=10 if day_only else 9,minute=d.minute,second=0,microsecond=0)
 return int(d.timestamp())


def label_time(ts):return datetime.fromtimestamp(ts,MSK).strftime('%d.%m.%Y %H:%M МСК')


def parse_when(text,now):
 """Conservative date parser: never invent a day from a month or a vague agreement."""
 text=' '.join(text.casefold().replace('ё','е').split());tz=MSK
 if re.search(r'дуба|оаэ|utc\s*\+?4|gmt\s*\+?4',text):tz=ZoneInfo('Asia/Dubai')
 elif re.search(r'часов(?:ом|ой)? пояс|по местному|по нашему|utc|gmt',text) and not re.search(r'мск|моск|utc\s*\+?3|gmt\s*\+?3',text):raise ValueError('Уточните часовой пояс')
 if re.search(r'екатеринбург|новосибирск|красноярск|иркутск|омск|калининград|владивосток|самар|челябинск',text):raise ValueError('Уточните время в МСК или по Дубаю')
 n=datetime.fromtimestamp(now,tz);date=None
 h=re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b',text)
 hour,minute=(int(h[1]),int(h[2])) if h else (10,0)
 if not h:
  h=re.search(r'\bв\s+([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|утра|вечера|мск|по москве|по дубаю|$)(?!\w)',text)
  if h:hour=int(h[1]);hour+=12 if 'вечера' in h[0] and hour<12 else 0
 explicit=re.search(r'\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b',text)
 if explicit:
  year=int(explicit[3]) if explicit[3] else n.year
  date=datetime(year,int(explicit[2]),int(explicit[1]),hour,minute,tzinfo=tz)
  if not explicit[3] and (date.month,date.day)<(n.month,n.day):date=date.replace(year=year+1)
 elif 'послезавтра' in text:date=(n+timedelta(days=2)).replace(hour=hour,minute=minute,second=0,microsecond=0)
 elif 'завтра' in text:date=(n+timedelta(days=1)).replace(hour=hour,minute=minute,second=0,microsecond=0)
 elif re.search(r'\bсегодня\b',text):date=n.replace(hour=hour,minute=minute,second=0,microsecond=0)
 else:
  weeks=re.search(r'через\s+(?:(\d+)\s+)?недел',text)
  months=re.search(r'через\s+(?:(\d+)\s+)?месяц',text)
  days=re.search(r'через\s+(\d+)\s+д',text)
  names={'понедельник':0,'вторник':1,'среду':2,'четверг':3,'пятницу':4,'субботу':5,'воскресенье':6}
  weekday=next((v for k,v in names.items() if k in text),None)
  if weeks:date=(n+timedelta(days=7*int(weeks[1] or 1))).replace(hour=hour,minute=minute,second=0,microsecond=0)
  elif months:date=datetime.fromtimestamp(month_add(now,int(months[1] or 1)),tz).replace(hour=hour,minute=minute,second=0,microsecond=0)
  elif days:date=(n+timedelta(days=int(days[1]))).replace(hour=hour,minute=minute,second=0,microsecond=0)
  elif weekday is not None:
   if re.search(r'следующ(?:ую|ей)\s+',text):raise ValueError('Уточните дату следующей недели')
   delta=(weekday-n.weekday())%7;date=(n+timedelta(days=delta)).replace(hour=hour,minute=minute,second=0,microsecond=0)
   if date.timestamp()<=now:date+=timedelta(days=7)
  else:
   months_names=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
   match=re.search(r'\b(\d{1,2})\s+('+'|'.join(months_names)+r')\b',text)
   if match:
    date=datetime(n.year,months_names.index(match[2])+1,int(match[1]),hour,minute,tzinfo=tz)
    if date.timestamp()<=now:date=date.replace(year=n.year+1)
 if date is None:raise ValueError('Нужны конкретная дата и, при необходимости, время')
 ts=int(date.timestamp())
 if ts<=now:raise ValueError('Дата уже прошла')
 if not working(ts):raise ValueError('Выберите рабочий день с 09:00 до 18:00 МСК')
 return ts


def bound(store,tid,actor,manager,revision=None,*,allow_manual=True,allow_stopped=False):
 t=store.thread(tid)
 if (not account_allowed(actor) or not thread_allowed(t) or t['account_id']!=actor or t['manager_id']!=manager or
     t['company_id']!='dvk' or t['business_id']!='dvk_finance' or not t['seller_enabled']):raise PermissionError('Календарь доступен только владельцу ДВК Финанс')
 if revision is not None and t['revision']!=revision:raise StaleDialogue('Карточка диалога изменилась')
 if not allow_stopped and (t['state'] in {'closed','opt_out'} or not t['outgoing_allowed']):raise ValueError('Контакт закрыт или действует запрет')
 if not allow_stopped and not t['history_complete']:raise ValueError('Переписка ещё не проверена полностью')
 if not allow_stopped and store.unresolved_actions(tid):raise ValueError('Сначала проверьте неопределённую отправку')
 return t


def had_application_handoff(store,tid):
 with store.connection() as db:
  app=db.execute("SELECT MIN(created_at) FROM seller_applications WHERE thread_id=? AND quality='valid'",(tid,)).fetchone()[0]
  if app is None:return False
  notices={(r['event_type'],r['created_at']) for r in db.execute("SELECT event_type,created_at FROM dialogue_notices WHERE thread_id=? AND event_type IN ('ready','call','documents','communication_task') AND created_at>=?",(tid,app))}
  for r in db.execute("SELECT data,created_at FROM dialogue_control_events WHERE thread_id=? AND event='handoff_paused' AND created_at>=?",(tid,app)):
   try:event_type=json.loads(r['data']).get('event_type')
   except (TypeError,ValueError):continue
   if (event_type,r['created_at']) in notices:return True
  return False


def classify(store,thread,history):
 if thread['state'] in {'closed','opt_out'} or not thread['outgoing_allowed']:return 'excluded','Запрет клиента или закрытая история'
 if store.unresolved_actions(thread['id']):return 'manual','Сначала проверить неопределённую отправку'
 if had_application_handoff(store,thread['id']):return 'needs_return','После заявки: требуется явный возврат МОП на прогрев'
 if thread['mode'] in {'off','copilot','paused_until'} or thread['state']=='human':return 'manual','Диалог у МОП или на ручной паузе'
 incoming=[m for m in history if m['direction']=='incoming' and m['sent_at']>=thread['first_sent_at']]
 return ('conversation','Был ответ, передачи заявки МОП не было') if incoming else ('silent','На первичное обращение ответа не было')


def cancel_db(db,tid,now,reason,*,pause=True):
 if not exists(db):return
 db.execute("UPDATE seller_followups SET status='sent',updated_at=? WHERE thread_id=? AND status='queued' AND action_id IN (SELECT id FROM dialogue_actions WHERE status='sent')",(int(now),tid))
 db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE id IN (SELECT action_id FROM seller_followups WHERE thread_id=? AND category!='manager' AND status IN ('proposed','scheduled','preparing','queued')) AND status='pending'",(tid,))
 db.execute("UPDATE seller_followups SET status='cancelled',reason=?,revision=revision+1,updated_at=? WHERE thread_id=? AND category!='manager' AND status IN ('proposed','scheduled','preparing','queued')",(reason,int(now),tid))
 if pause:db.execute("UPDATE seller_warm_cohorts SET status='paused',revision=revision+1,updated_at=? WHERE thread_id=?",(int(now),tid))
 audit(db,tid,'cancelled',now,reason=reason)


def on_message_db(db,thread,*,direction,source,now):
 if not exists(db) or source=='history' or not thread_allowed(thread) or thread['company_id']!='dvk':return
 if direction=='incoming' or source=='manager':
  cancel_db(db,thread['id'],now,'Новое входящее' if direction=='incoming' else 'Ручной перехват')
  if direction=='incoming':db.execute("UPDATE seller_followups SET status='blocked',reason='Клиент написал снова: проверьте договорённость',revision=revision+1 WHERE thread_id=? AND category='manager' AND status='scheduled'",(thread['id'],))
  if direction=='incoming' and thread['mode'] in {'nurture','silent'} and thread['state'] not in {'closed','opt_out','human'}:
   db.execute("UPDATE dialogue_threads SET mode='active_negotiation',control_reason='Клиент ответил. Прогрев остановлен, продолжаются переговоры.' WHERE id=?",(thread['id'],))


def _insert(db,tid,category,kind,due,purpose,status,created_by,now,source_id=None,quote='',proof=None):
 id=uuid.uuid4().hex
 db.execute('''INSERT INTO seller_followups(id,thread_id,category,kind,due_at,purpose,status,source_message_id,source_quote,proof,created_by,created_at,updated_at)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(id,tid,category,kind,int(due),purpose,status,source_id,quote,json.dumps(proof or {},ensure_ascii=False),created_by,int(now),int(now)))
 audit(db,tid,'created',now,task_id=id,category=category,kind=kind,due_at=int(due),status=status)
 return id


def next_regular(category,base,cycle,tid):
 if category=='post_application':return business_at(base+10*86400,tid,day_only=True),('product' if cycle%3==2 else 'social')
 if category=='conversation':return business_at(month_add(base,1),tid,day_only=True),'reconnect'
 if category=='silent':return business_at(month_add(base,3),tid,day_only=True),('final_social' if cycle>=2 else 'product')
 raise ValueError('Неизвестная группа')


def assign(store,tid,category,*,actor,manager,revision,live=False):
 if category not in GROUPS:raise ValueError('Неизвестная группа')
 initialize(store);t=bound(store,tid,actor,manager,revision);_,history=store.snapshot(tid)
 if category=='post_application' and not had_application_handoff(store,tid):raise ValueError('Не было подтверждённой передачи заявки МОП')
 with store.connection() as db:
  audited=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='seller_history_audit'").fetchone() and db.execute("SELECT 1 FROM seller_history_audit WHERE thread_id=? AND verified=1",(tid,)).fetchone())
 incoming=[m for m in history if m['direction']=='incoming' and (audited or m['sent_at']>=t['first_sent_at'])]
 if category=='silent' and incoming:raise ValueError('Клиент уже отвечал: это не молчащий')
 if category=='conversation' and (not incoming or had_application_handoff(store,tid)):raise ValueError('Этой группе нужны ответы без передачи заявки МОП')
 if category!='post_application' and (t['state']=='human' or t['mode'] in {'copilot','off','paused_until'}):raise ValueError('Сначала отдельно верните диалог ИИ')
 now=int(store.clock());base=now if category=='post_application' else max([t['first_sent_at']]+[m['sent_at'] for m in history])
 due,kind=next_regular(category,base,0,tid)
 if due<=now:due=business_at(now+86400,tid,day_only=True)
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  current=store._thread(db,tid)
  if current['revision']!=revision:raise StaleDialogue('Диалог изменился')
  if db.execute("SELECT 1 FROM seller_followups WHERE thread_id=? AND category='agreed' AND status IN ('proposed','scheduled','preparing','queued')",(tid,)).fetchone():raise ValueError('Сначала выполните или отмените согласованную дату клиента')
  existing=db.execute("SELECT * FROM seller_warm_cohorts WHERE thread_id=?",(tid,)).fetchone()
  if existing and existing['category']==category and existing['status'] in {'active','proposed'}:raise ValueError('Эта группа уже назначена: дубль не создан')
  cancel_db(db,tid,now,'Назначена группа прогрева')
  db.execute('''INSERT INTO seller_warm_cohorts(thread_id,category,status,returned_at,created_at,updated_at)
   VALUES(?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET category=excluded.category,status=excluded.status,
   returned_at=excluded.returned_at,cycle=0,last_touch_at=NULL,revision=revision+1,updated_at=excluded.updated_at''',
   (tid,category,'active' if live else 'proposed',now if category=='post_application' else None,now,now))
  # This explicit button is distinct from Return AI. Old application turns cannot replay.
  if category=='post_application':
   floor=max([t['incoming_floor_id']]+[m['telegram_id'] for m in incoming])
   db.execute("UPDATE dialogue_threads SET incoming_floor_id=?,processed_incoming_id=MAX(processed_incoming_id,?),resume_pending=0 WHERE id=?",(floor,floor,tid))
  db.execute("UPDATE dialogue_threads SET mode=?,state='engaged',resume_pending=0,next_contact_at=NULL,control_epoch=control_epoch+1,revision=revision+1,control_reason=?,updated_at=? WHERE id=?",
   ('silent' if category=='silent' else 'nurture','Прогрев: '+GROUPS[category]+('. Отправки разрешены.' if live else '. План ожидает разрешения на отправки.'),now,tid))
  return _insert(db,tid,category,kind,due,GROUPS[category],'scheduled' if live else 'proposed','owner',now)


def agreed(store,tid,*,source_id,quote,due,purpose,live=False,proof=None):
 initialize(store);t=store.thread(tid);bound(store,tid,t['account_id'],t['manager_id'])
 if not working(due) or due<=store.clock():raise ValueError('Нужна будущая дата в рабочее время')
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  source=db.execute("SELECT * FROM dialogue_messages WHERE thread_id=? AND telegram_id=? AND direction='incoming'",(tid,source_id)).fetchone()
  if not source or not quote or quote not in source['text']:raise ValueError('Нет слов клиента с договорённостью')
  prior=db.execute("SELECT * FROM seller_followups WHERE thread_id=? AND category='agreed' AND source_message_id=?",(tid,source_id)).fetchone()
  if prior:return prior['id']
  cancel_db(db,tid,store.clock(),'Конкретная дата клиента имеет приоритет')
  return _insert(db,tid,'agreed','agreed',due,purpose,'scheduled' if live else 'proposed','customer',store.clock(),source_id,quote,proof)


def owner_task(store,tid,*,actor,manager,revision,when,purpose,kind='agreed',live=False):
 initialize(store);t=bound(store,tid,actor,manager,revision)
 if kind not in {'agreed','manager_call','manager_email','manager_message'}:raise ValueError('Неизвестный вид задачи')
 if not isinstance(purpose,str) or not 1<=len(purpose.strip())<=700:raise ValueError('Укажите цель до 700 символов')
 due=parse_when(when,store.clock())
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  if store._thread(db,tid)['revision']!=revision:raise StaleDialogue('Диалог изменился')
  previous=db.execute("SELECT id FROM seller_followups WHERE thread_id=? AND kind=? AND due_at=? AND purpose=? AND status IN ('proposed','scheduled','preparing','queued')",(tid,kind,due,purpose.strip())).fetchone()
  if previous:return previous['id']
  cancel_db(db,tid,store.clock(),'Назначена задача владельца')
  return _insert(db,tid,'manager' if kind.startswith('manager_') else 'agreed',kind,due,purpose.strip(),
    'scheduled' if live or kind.startswith('manager_') else 'proposed','owner',store.clock())


def task_change(store,id,*,actor,manager,revision,action,when=None):
 initialize(store)
 with store.connection() as db:r=db.execute('SELECT * FROM seller_followups WHERE id=?',(id,)).fetchone()
 if not r:raise ValueError('Задача не найдена')
 bound(store,r['thread_id'],actor,manager,allow_stopped=action in {'cancel','done'})
 if r['revision']!=revision or r['status'] not in (*OPEN,'blocked'):raise StaleDialogue('Задача уже изменилась')
 due=parse_when(when,store.clock()) if action=='move' else r['due_at']
 if action not in {'move','cancel','done'}:raise ValueError('Неизвестное действие')
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  current=db.execute('SELECT * FROM seller_followups WHERE id=?',(id,)).fetchone()
  if current['revision']!=revision:raise StaleDialogue('Задача изменилась')
  if r['action_id']:
   sent=db.execute('SELECT status FROM dialogue_actions WHERE id=?',(r['action_id'],)).fetchone()
   if sent and sent[0] in {'sending','sent','uncertain'}:raise ValueError('Нельзя изменить выполняющуюся или неопределённую отправку')
   db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE id=? AND status='pending'",(r['action_id'],))
  status=('proposed' if current['status']=='proposed' else 'scheduled') if action=='move' else ('done' if action=='done' else 'cancelled')
  db.execute('UPDATE seller_followups SET due_at=?,status=?,action_id=NULL,revision=revision+1,updated_at=?,reason=? WHERE id=?',(due,status,int(store.clock()),'Действие владельца: '+action,id))
  audit(db,r['thread_id'],action,store.clock(),task_id=id,due_at=due)


def list_tasks(store,account_id,manager_id,*,limit=20,offset=0):
 initialize(store)
 with store.connection() as db:
  return [dict(r) for r in db.execute("SELECT f.* FROM seller_followups f JOIN dialogue_threads t ON t.id=f.thread_id WHERE t.account_id=? AND t.manager_id=? AND f.status IN ('proposed','scheduled','preparing','queued','blocked') ORDER BY f.due_at,f.id LIMIT ? OFFSET ?",(account_id,manager_id,limit,offset))]


def quota_ok(db,tid,category,kind,now):
 start=datetime.fromtimestamp(now,MSK).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
 end=datetime.fromtimestamp(month_add(int(start.timestamp()),1),MSK)
 rows=db.execute("SELECT f.kind,a.sent_at FROM seller_followups f JOIN dialogue_actions a ON a.id=f.action_id WHERE f.thread_id=? AND f.category=? AND a.status='sent' AND a.sent_at>=? AND a.sent_at<?",(tid,category,int(start.timestamp()),int(end.timestamp()))).fetchall()
 if category=='post_application':return len(rows)<3 and sum(r['kind']==kind for r in rows)<(1 if kind=='product' else 2)
 if category=='conversation':return len(rows)<1
 if category=='silent':
  last=db.execute("SELECT MAX(a.sent_at) FROM seller_followups f JOIN dialogue_actions a ON a.id=f.action_id WHERE f.thread_id=? AND f.category='silent' AND a.status='sent'",(tid,)).fetchone()[0]
  return last is None or month_add(last,3)<=now
 return True


def guard_action(db,thread,action,now):
 """Final transactional send gate, including a direct restart or late queue delivery."""
 if action['kind']!='calendar':return
 if not exists(db):raise StaleDialogue('Календарь выключен')
 enabled=db.execute("SELECT value FROM seller_calendar_meta WHERE key='live_enabled'").fetchone()
 if not enabled or enabled[0]!='true':raise StaleDialogue('План ещё не разрешён владельцем')
 test_peer=db.execute("SELECT value FROM seller_calendar_meta WHERE key='test_peer_id'").fetchone()
 if test_peer and test_peer[0] and str(thread['peer_id'])!=test_peer[0]:raise StaleDialogue('Сейчас разрешён только тестовый контакт')
 task=db.execute("SELECT * FROM seller_followups WHERE action_id=? AND status='queued'",(action['id'],)).fetchone()
 if not task or task['due_at']>now or not working(now):raise StaleDialogue('Календарная отправка сейчас не разрешена')
 if task['kind'] in {'manager_call','manager_email'} or task['category']=='manager':raise StaleDialogue('Эту задачу выполняет только МОП')
 if not thread_allowed(thread) or thread['company_id']!='dvk' or thread['business_id']!='dvk_finance':raise StaleDialogue('Неверная область календаря')
 if task['category']=='agreed' and now-task['due_at']>3600:
  raise StaleDialogue('Согласованное время пропущено: нужна проверка')
 if task['category'] not in {'agreed','manager'}:
  cohort=db.execute('SELECT * FROM seller_warm_cohorts WHERE thread_id=?',(thread['id'],)).fetchone()
  if not cohort or cohort['status']!='active':raise StaleDialogue('Прогрев приостановлен')
 if not quota_ok(db,thread['id'],task['category'],task['kind'],now):raise StaleDialogue('Исчерпана частота касаний')
 if db.execute("SELECT 1 FROM seller_followups f JOIN dialogue_actions a ON a.id=f.action_id WHERE a.status='sent' AND a.sent_at>?",(int(now)-120,)).fetchone():raise StaleDialogue('Плановые касания распределяются по времени')


def recover_and_reconcile(store,*,recover=False):
 initialize(store);now=int(store.clock())
 with store.connection() as db:
  # A queued action survives restart. Never duplicate sent/sending/uncertain actions.
  if recover:
   db.execute("UPDATE seller_followups SET status='queued',action_id=(SELECT a.id FROM dialogue_actions a WHERE a.thread_id=seller_followups.thread_id AND a.action_key='calendar:'||seller_followups.id) WHERE status='preparing' AND EXISTS (SELECT 1 FROM dialogue_actions a WHERE a.thread_id=seller_followups.thread_id AND a.action_key='calendar:'||seller_followups.id)")
   db.execute("UPDATE seller_followups SET status='scheduled',revision=revision+1,updated_at=? WHERE status='preparing' AND action_id IS NULL",(now,))
  rows=db.execute("SELECT f.*,a.status astatus,a.sent_at FROM seller_followups f JOIN dialogue_actions a ON a.id=f.action_id WHERE f.status='queued' AND a.status IN ('sent','cancelled','not_sent','uncertain')").fetchall()
  for r in rows:
   if r['astatus']=='sent':
    db.execute("UPDATE seller_followups SET status='sent',revision=revision+1,updated_at=? WHERE id=?",(now,r['id']))
    if r['category'] in GROUPS:
     c=db.execute('SELECT * FROM seller_warm_cohorts WHERE thread_id=?',(r['thread_id'],)).fetchone()
     if not c:continue
     cycle=c['cycle']+1;last=r['sent_at']
     if r['category']=='silent' and r['kind']=='final_social':
      db.execute("UPDATE seller_warm_cohorts SET status='stopped',cycle=?,last_touch_at=?,revision=revision+1,updated_at=? WHERE thread_id=?",(cycle,last,now,r['thread_id']));continue
     db.execute('UPDATE seller_warm_cohorts SET cycle=?,last_touch_at=?,revision=revision+1,updated_at=? WHERE thread_id=?',(cycle,last,now,r['thread_id']))
     if c['status']=='active':
      due,kind=next_regular(c['category'],last,cycle,r['thread_id'])
      _insert(db,r['thread_id'],c['category'],kind,due,GROUPS[c['category']],'scheduled','cycle',now)
   else:
    db.execute("UPDATE seller_followups SET status=?,reason=?,revision=revision+1,updated_at=? WHERE id=?",('blocked' if r['astatus']=='uncertain' else 'cancelled','Статус отправки: '+r['astatus'],now,r['id']))


def due_threads(store):
 with store.connection() as db:
  if not exists(db):return set()
  return {r[0] for r in db.execute("SELECT DISTINCT thread_id FROM seller_followups WHERE status IN ('scheduled','queued') AND kind NOT LIKE 'manager_%' AND due_at<=?",(int(store.clock()),))}


async def tick(worker,settings):
 """Prepare one sourced touch under the chat lock. Global live switch defaults OFF."""
 if not settings.get('stage3_live_enabled'):return
 if settings.get('finance_managed'):
  from finance_priority import calendar_allowed
  if not await calendar_allowed(worker):return
 store=worker.store;tid=worker.thread_id;now=int(store.clock())
 if settings.get('stage3_test_peer_id') and store.thread(tid)['peer_id']!=settings['stage3_test_peer_id']:return
 if not working(now):return
 t=store.thread(tid)
 if not can_send(t) or t['state'] in {'human','closed','opt_out'}:return
 recover_and_reconcile(store)
 with store.connection() as db:
  enabled=db.execute("SELECT value FROM seller_calendar_meta WHERE key='live_enabled'").fetchone()
  if not enabled or enabled[0]!='true':return
  job=db.execute("SELECT * FROM seller_followups WHERE thread_id=? AND status='scheduled' AND kind NOT LIKE 'manager_%' AND due_at<=? ORDER BY CASE WHEN category='agreed' THEN 0 ELSE 1 END,due_at LIMIT 1",(tid,now)).fetchone()
  if not job:return
  job=dict(job)
  if job['category'] in GROUPS:
   cohort=db.execute('SELECT status FROM seller_warm_cohorts WHERE thread_id=?',(tid,)).fetchone()
   if not cohort or cohort[0]!='active':return
  if job['category'] in GROUPS and not quota_ok(db,tid,job['category'],job['kind'],now):return
  if db.execute("SELECT 1 FROM seller_followups f JOIN dialogue_actions a ON a.id=f.action_id WHERE a.status='sent' AND a.sent_at>?",(now-120,)).fetchone():return
  # Overdue regular jobs are re-spaced; a requested date gets review rather than a false promise.
  if now-job['due_at']>(3600 if job['category']=='agreed' else 86400):
   if job['category']=='agreed':db.execute("UPDATE seller_followups SET status='blocked',reason='Пропущена согласованная дата. Проверьте задачу.',revision=revision+1 WHERE id=?",(job['id'],))
   else:db.execute('UPDATE seller_followups SET due_at=?,revision=revision+1 WHERE id=?',(business_at(now+86400,tid,day_only=True),job['id']))
   return
 if await worker.refresh(full=bool(settings.get('stage3_verified_base'))):worker.changed.set();return
 if not await worker.verify_visible_history():return
 # Every history scan, edit or manager takeover can invalidate the proposed turn.
 t,history=worker.engine._snapshot(tid);revision=t['revision']
 with store.connection() as db:
  if db.execute("UPDATE seller_followups SET status='preparing' WHERE id=? AND revision=? AND status='scheduled'",(job['id'],job['revision'])).rowcount!=1:return
 from dialogue_knowledge import KnowledgeAnswer,KnowledgeUnavailable
 try:
  if job['kind']=='product':
   knowledge=await worker.remote.lookup(business_id=t['business_id'],product=t['product'],question='Какие подтверждённые публичные возможности ДВК Финанс по этому продукту уместно кратко напомнить клиенту? Не включать внутренние комиссии и непроверенные условия.',request_id=uuid.uuid4().hex)
   if not knowledge.evidence:raise KnowledgeUnavailable('Нет подтверждённых источников для продуктового касания')
   source=' '.join(e.text for e in knowledge.evidence).casefold()
   if any(p.casefold() in source for p in worker.policy.restricted_knowledge_markers):
    from dialogue_knowledge import public_document_answer
    knowledge=public_document_answer(knowledge,worker.policy,'Публичные возможности ДВК Финанс')
    if knowledge is None:raise KnowledgeUnavailable('Нужна проверка публичности условий')
  else:knowledge=KnowledgeAnswer('Только ненавязчиво поддержать контакт или вернуться к согласованной теме. Никаких новых ставок, новостей, обещаний или банковских условий.',(),uuid.uuid4().hex)
  stage=dict(calendar=True,category=job['category'],kind=job['kind'],purpose=job['purpose'],source_quote=job['source_quote'])
  text=await worker.engine.writer.compose(thread=t,history=history,decision=None,knowledge=knowledge,stage=stage)
  worker.engine._check(tid,revision)
  with store.connection() as db:
   current=db.execute('SELECT status,revision FROM seller_followups WHERE id=?',(job['id'],)).fetchone()
   if not current or current['status']!='preparing' or current['revision']!=job['revision']:raise StaleDialogue('Задача уже изменилась')
  action_id=store.queue_action(tid,action_key='calendar:'+job['id'],kind='calendar',text=text,expected_revision=revision,due_at=now)
  with store.connection() as db:
   db.execute("UPDATE seller_followups SET status='queued',draft=?,action_id=?,updated_at=? WHERE id=? AND status='preparing'",(text,action_id,now,job['id']))
   audit(db,tid,'prepared',now,task_id=job['id'],action_id=action_id,knowledge_request_id=getattr(knowledge,'request_id',None),sources=[dict(file_id=e.file_id,filename=e.filename,sha256=hashlib.sha256(e.text.encode()).hexdigest()) for e in knowledge.evidence])
 except asyncio.CancelledError:
  with store.connection() as db:db.execute("UPDATE seller_followups SET status='scheduled' WHERE id=? AND status='preparing' AND action_id IS NULL",(job['id'],))
  raise
 except Exception as exc:
  with store.connection() as db:
   db.execute("UPDATE seller_followups SET status='blocked',reason=?,revision=revision+1,updated_at=? WHERE id=? AND status='preparing'",('Нужна проверка: '+type(exc).__name__,now,job['id']))
  raise

def external_agreement(store,tid,history,plan):
 """Keep agreed calls/emails in the owner's calendar; never execute them automatically."""
 initialize(store);t=store.thread(tid);bound(store,tid,t['account_id'],t['manager_id'])
 if plan['channel'] not in {'phone','email','telegram'}:return None
 source=next((m for m in reversed(history) if m['direction']=='incoming'),None)
 if not source:return None
 try:due=parse_when(plan['when'],store.clock());status='scheduled';reason=''
 except ValueError:due=0;status='blocked';reason='Уточнить точную дату и время: '+(plan['when'] or '')
 kind='manager_call' if plan['action']=='call' else 'manager_email' if plan['channel']=='email' else 'manager_message'
 purpose=plan['purpose']+'; '+plan['recipient']+'; '+plan['channel']+'; '+(plan['address'] or 'текущий Telegram-диалог')
 with store.connection() as db:
  old=db.execute("SELECT id FROM seller_followups WHERE thread_id=? AND category='manager' AND source_message_id=?",(tid,source['telegram_id'])).fetchone()
  if old:return old['id']
  id=_insert(db,tid,'manager',kind,due,purpose,status,'customer',store.clock(),source['telegram_id'],source['text'],plan)
  if reason:db.execute('UPDATE seller_followups SET reason=? WHERE id=?',(reason,id))
  return id


def review_plan(store):
 """Read-only recipient list: no cohort assignment, mode change, generation or sending."""
 from dialogue_runtime import future_contacts
 with store.connection() as db:
  contacts=future_contacts(db,current_manager(),0,True)
  threads={r['peer_id']:dict(r) for r in db.execute("SELECT * FROM dialogue_threads WHERE account_id=? AND manager_id=? AND company_id='dvk'",(current_account(),current_manager()))}
  names={}
  for r in db.execute("SELECT telegram_id,full_name,username FROM clients WHERE manager_id=? ORDER BY id DESC",(current_manager(),)):
   names.setdefault(r['telegram_id'],dict(r))
 items=[];now=int(store.clock())
 for peer,product in contacts:
  t=threads.get(peer);name=names.get(peer,{})
  item=dict(peer_id=peer,name=name.get('full_name',''),username=name.get('username',''),product=product,
   thread_id=t['id'] if t else None,revision=t['revision'] if t else None)
  if t and (t['state'] in {'closed','opt_out'} or not t['outgoing_allowed']):
   category,reason='excluded','Запрет клиента или закрытая история';due=None
  elif not t or not t['history_complete'] or not t['seller_enabled']:
   category,reason='needs_history','История ещё не подключена/не проверена, в автоматический запуск не включать'
   due=None
  else:
   _,history=store.snapshot(t['id']);category,reason=classify(store,t,history);due=None
   if category in {'conversation','silent'}:
    base=max([t['first_sent_at']]+[m['sent_at'] for m in history])
    due,kind=next_regular(category,base,0,t['id'])
    if due<=now:due=business_at(now+86400,t['id'],day_only=True)
    item['kind']=kind
  item.update(category=category,reason=reason,due_at=due,due_msk=label_time(due) if due else None)
  items.append(item)
 value=dict(created_at=now,groups=GROUPS,items=items,live_enabled=False)
 value['sha256']=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
 return value


def apply_reviewed_plan(store,plan,selected_peers,*,actor,manager):
 """Only call after explicit owner approval of this exact saved list; does not enable sending."""
 if not account_allowed(actor) or manager!=current_manager():raise PermissionError('Только владелец')
 unsigned={k:v for k,v in plan.items() if k!='sha256'}
 if hashlib.sha256(json.dumps(unsigned,ensure_ascii=False,sort_keys=True).encode()).hexdigest()!=plan['sha256']:raise ValueError('План изменён после просмотра')
 selected=set(selected_peers);eligible={i['peer_id']:i for i in plan['items'] if i['category'] in {'conversation','silent'}}
 if not selected<=set(eligible):raise ValueError('Список содержит неготовые или вручную остановленные контакты')
 result=[]
 # Preflight every recipient before making any changes.
 for peer in selected:
  i=eligible[peer];bound(store,i['thread_id'],actor,manager,i['revision'])
 for peer in sorted(selected):
  i=eligible[peer];result.append(assign(store,i['thread_id'],i['category'],actor=actor,manager=manager,revision=i['revision'],live=False))
 return result


def activate_reviewed_plan(store,plan,selected_peers,*,actor,manager):
 """Separate explicit approval step. Caller must also enable matching runtime config.
 Never invoke during installation, tests of the live database, or preview generation.
 """
 selected=set(selected_peers)
 if not selected:raise ValueError('Список получателей пуст')
 for i in plan['items']:
  if i['peer_id'] in selected and (not i.get('due_at') or i['due_at']<=store.clock()):raise ValueError('Даты плана устарели: нужен новый просмотр')
 ids=apply_reviewed_plan(store,plan,selected,actor=actor,manager=manager)
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  for id in ids:
   row=db.execute('SELECT * FROM seller_followups WHERE id=?',(id,)).fetchone()
   target=next(i for i in plan['items'] if i['thread_id']==row['thread_id'])
   if row['due_at']!=target['due_at'] or row['status']!='proposed':raise StaleDialogue('Дата или задача изменилась: план не включён')
  for id in ids:
   row=db.execute('SELECT thread_id FROM seller_followups WHERE id=?',(id,)).fetchone()
   db.execute("UPDATE seller_followups SET status='scheduled',revision=revision+1 WHERE id=?",(id,))
   db.execute("UPDATE seller_warm_cohorts SET status='active',revision=revision+1 WHERE thread_id=?",(row['thread_id'],))
   audit(db,row['thread_id'],'owner_approved_plan',store.clock(),task_id=id,actor=actor,plan_sha256=plan['sha256'])
  db.execute("INSERT OR REPLACE INTO seller_calendar_meta VALUES('live_enabled','true')")
 return ids

def resume_after_reply(store,tid,settings):
 """Owner-approved policy: restart the proper interval after a handled conversation turn."""
 if not settings.get('stage3_verified_base') or not settings.get('stage3_live_enabled'):return
 initialize(store)
 t=store.thread(tid)
 if t['state']!='engaged' or t['mode'] not in {'active_negotiation','nurture','silent'} or not can_send(t):return
 with store.connection() as db:
  enabled=db.execute("SELECT value FROM seller_calendar_meta WHERE key='live_enabled'").fetchone()
  if not enabled or enabled[0]!='true':return
  a=db.execute("SELECT disposition FROM seller_history_audit WHERE thread_id=? AND verified=1",(tid,)).fetchone()
  if settings.get('finance_managed'):
   # Managed enrollment verifies actual Telegram history before assigning a cohort.
   # A private WORK audit is not a prerequisite on another manager's fresh database.
   if not t['history_complete']:return
   if a and a[0] not in {'conversation','silent','agreed'}:return
  elif not a or a[0] not in {'conversation','silent','agreed'}:return
  cohort=db.execute('SELECT * FROM seller_warm_cohorts WHERE thread_id=?',(tid,)).fetchone()
  if not cohort or cohort['status']!='paused':return
  if db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status IN ('pending','sending','uncertain')",(tid,)).fetchone():return
  if db.execute("SELECT 1 FROM seller_followups WHERE thread_id=? AND status IN ('proposed','scheduled','preparing','queued')",(tid,)).fetchone():return
  latest=db.execute("SELECT * FROM dialogue_messages WHERE thread_id=? AND direction='incoming' ORDER BY telegram_id DESC LIMIT 1",(tid,)).fetchone()
  if not latest or latest['telegram_id']>t['processed_incoming_id']:return
  group='conversation' if cohort['category']=='silent' else cohort['category']
 if group!='post_application' and had_application_handoff(store,tid):return
 now=int(store.clock());due,kind=next_regular(group,now,cohort['cycle'] if group=='post_application' else 0,tid)
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  if store._thread(db,tid)['revision']!=t['revision']:return
  changed=db.execute("UPDATE seller_warm_cohorts SET category=?,status='active',revision=revision+1,updated_at=? WHERE thread_id=? AND status='paused'",(group,now,tid)).rowcount
  if changed:_insert(db,tid,group,kind,due,GROUPS[group],'scheduled','after_reply_policy',now)

def ensure_audit_table(store):
 with store.connection() as db:
  db.execute('''CREATE TABLE IF NOT EXISTS seller_history_audit (
   peer_id INTEGER PRIMARY KEY,thread_id TEXT NOT NULL,account_id INTEGER NOT NULL,company_id TEXT NOT NULL,
   first_message_id INTEGER NOT NULL,audited_at INTEGER NOT NULL,verified INTEGER NOT NULL,
   message_count INTEGER NOT NULL,history_sha256 TEXT NOT NULL,disposition TEXT NOT NULL,reason TEXT NOT NULL)''')
