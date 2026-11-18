from finance_scope import account_allowed, thread_allowed, current_account, current_manager
"""Local owner-pilot application ledger. No Telegram operations or send permissions."""
import json
import uuid
from dialogue_store import StaleDialogue
from dialogue_seller import OWNER_ACCOUNT_ID
from dialogue_memory import FIELDS,FACT_SCHEMA

SUCCESS={'need':'Подтверждена конкретная потребность','calculation':'Запрошен расчёт/предложение',
         'documents':'Готов предоставить документы','call':'Согласован разговор с МОП',
         'contact':'Передан контакт ответственного'}
EVIDENCE={'type':'object','additionalProperties':False,'properties':{
    'source_message_id':{'type':'integer'},'quote':{'type':'string'}},'required':['source_message_id','quote']}
APPLICATION_SCHEMA={'type':['object','null'],'additionalProperties':False,'properties':{
    'application_id':{'type':['string','null']},'separate_need':{'type':'boolean'},
    'product':{'type':'string'},'subject':{'type':'string'},
    'need_evidence':EVIDENCE,'separation_evidence':{'anyOf':[EVIDENCE,{'type':'null'}]},
    'qualification':{'type':'array','items':FACT_SCHEMA},
    'success':{'type':'array','items':{'type':'object','additionalProperties':False,'properties':{
        'criterion':{'type':'string','enum':list(SUCCESS)},'evidence':EVIDENCE},'required':['criterion','evidence']}}},
    'required':['application_id','separate_need','product','subject','need_evidence','separation_evidence','success','qualification']}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS seller_applications (
        id TEXT PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),
        product TEXT NOT NULL,subject TEXT NOT NULL,need_source_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',revision INTEGER NOT NULL DEFAULT 1,
        quality TEXT NOT NULL DEFAULT 'valid',created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
        UNIQUE(thread_id,product,need_source_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS seller_application_events (
        id TEXT PRIMARY KEY,application_id TEXT NOT NULL REFERENCES seller_applications(id),
        event_key TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,
        UNIQUE(application_id,event_key))''')
    if 'qualification' not in {r[1] for r in db.execute('PRAGMA table_info(seller_applications)')}:
        db.execute("ALTER TABLE seller_applications ADD COLUMN qualification TEXT NOT NULL DEFAULT '{}'")


def _bound(thread):
    if not thread_allowed(thread) or not thread['seller_enabled']:
        raise PermissionError('Заявки доступны только в пилоте владельца.')


def _evidence(value,messages):
    if not isinstance(value,dict) or set(value)!={'source_message_id','quote'}:raise ValueError('Invalid application evidence')
    if type(value['source_message_id']) is not int:raise ValueError('Invalid evidence source')
    message=messages.get(value['source_message_id'])
    quote=value['quote']
    if (message is None or message['direction']!='incoming' or not isinstance(quote,str)
            or not 0<len(quote)<=2000 or quote not in message['text']):
        raise ValueError('Application requires customer evidence')
    return dict(value,source_text=message['text'])


def _event(db,app_id,key,kind,data,now):
    encoded=json.dumps(data,ensure_ascii=False,sort_keys=True)
    previous=db.execute('SELECT kind,data FROM seller_application_events WHERE application_id=? AND event_key=?',(app_id,key)).fetchone()
    if previous:
        if tuple(previous)!=(kind,encoded):raise ValueError('Conflicting application replay')
        return
    db.execute('INSERT INTO seller_application_events VALUES (?,?,?,?,?,?)',
        (uuid.uuid4().hex,app_id,key,kind,encoded,now))


def record(store,thread,history,proposal,*,products,turn_id,review_epoch=0):
    _bound(thread)
    if proposal is None:return None
    required=set(APPLICATION_SCHEMA['required'])
    if not isinstance(proposal,dict) or not required-{'qualification'}<=set(proposal)<=required:raise ValueError('Invalid application proposal')
    if (proposal['product'] not in products or not isinstance(proposal['subject'],str)
            or not 0<len(proposal['subject'])<=500 or type(proposal['separate_need']) is not bool
            or not isinstance(proposal['success'],list) or len(proposal['success'])>5):raise ValueError('Invalid application fields')
    if proposal['application_id'] is not None and (not isinstance(proposal['application_id'],str)
            or len(proposal['application_id'])!=32):raise ValueError('Invalid application ID')
    messages={m['telegram_id']:m for m in history}
    if type(turn_id) is not int or turn_id not in messages or messages[turn_id]['direction']!='incoming':
        raise ValueError('Application turn must be an actual customer message')
    need=_evidence(proposal['need_evidence'],messages)
    facts=proposal.get('qualification',[])
    if not isinstance(facts,list) or len(facts)>20:raise ValueError('Invalid qualification')
    sourced={}
    for fact in facts:
        if (not isinstance(fact,dict) or set(fact)!=set(FACT_SCHEMA['required']) or fact['category']!='need'
                or fact['field'] not in FIELDS['need'] or not isinstance(fact['value'],str) or not 0<len(fact['value'])<=500):
            raise ValueError('Invalid qualification fact')
        source=_evidence({k:fact[k] for k in EVIDENCE['required']},messages)
        if fact['value'] not in source['quote'] or fact['field'] in sourced:raise ValueError('Unsourced or conflicting qualification')
        sourced[fact['field']]=dict(source,value=fact['value'])
    separation=_evidence(proposal['separation_evidence'],messages) if proposal['separation_evidence'] is not None else None
    if proposal['separate_need'] and separation is None:raise ValueError('Independent need requires explicit evidence')
    criteria=[]
    for item in proposal['success']:
        if not isinstance(item,dict) or set(item)!={'criterion','evidence'} or item['criterion'] not in SUCCESS:
            raise ValueError('Invalid success criterion')
        criteria.append(dict(criterion=item['criterion'],evidence=_evidence(item['evidence'],messages)))
    now=int(store.clock())
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=store._thread(db,thread['id'])
        if current['revision']!=thread['revision'] or current['mode']=='off':raise StaleDialogue('Диалог изменился до записи заявки.')
        for evidence in [need,separation,*[c['evidence'] for c in criteria],*sourced.values()]:
            if evidence is None:continue
            actual=db.execute('SELECT text FROM dialogue_messages WHERE thread_id=? AND telegram_id=?',
                (thread['id'],evidence['source_message_id'])).fetchone()
            if not actual or actual['text']!=evidence['source_text']:raise StaleDialogue('Источник заявки изменился.')
        app_id=proposal['application_id']
        if app_id is not None:
            app=db.execute('SELECT * FROM seller_applications WHERE id=? AND thread_id=?',(app_id,thread['id'])).fetchone()
            if not app or app['product']!=proposal['product'] or app['status']!='open' or proposal['separate_need']:
                raise ValueError('Application scope mismatch')
        else:
            app=db.execute('SELECT * FROM seller_applications WHERE thread_id=? AND product=? AND need_source_id=?',
                (thread['id'],proposal['product'],need['source_message_id'])).fetchone()
            if app and app['status']!='open':raise ValueError('Closed application requires manager review')
            if not app and not proposal['separate_need']:
                existing=db.execute("SELECT * FROM seller_applications WHERE thread_id=? AND product=? AND status='open'",
                    (thread['id'],proposal['product'])).fetchall()
                if len(existing)>1:raise ValueError('Specify the existing application')
                app=existing[0] if existing else None
            if not app:
                app_id=uuid.uuid4().hex
                db.execute('INSERT INTO seller_applications (id,thread_id,product,subject,need_source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                    (app_id,thread['id'],proposal['product'],proposal['subject'],need['source_message_id'],now,now))
            else:app_id=app['id']
        review_key='turn:'+str(turn_id)+(':review:'+str(review_epoch) if review_epoch else '')
        _event(db,app_id,review_key,'qualification',dict(subject=proposal['subject'],need=need,separation=separation,facts=sourced),now)
        previous_facts=json.loads(app['qualification']) if app else {}
        combined=dict(previous_facts)
        for field,fact in sourced.items():
            previous=previous_facts.get(field)
            if previous and previous['source_message_id']>fact['source_message_id']:continue
            if previous and previous['source_message_id']==fact['source_message_id']:
                if (previous['value'],previous['source_text'])!=(fact['value'],fact['source_text']):
                    raise ValueError('Conflicting qualification replay')
                continue
            combined[field]=fact
        if combined!=previous_facts:
            db.execute('UPDATE seller_applications SET qualification=?,revision=revision+1,updated_at=? WHERE id=?',
                (json.dumps(combined,ensure_ascii=False),now,app_id))
        if app and app['subject']!=proposal['subject']:
            db.execute('UPDATE seller_applications SET subject=?,revision=revision+1,updated_at=? WHERE id=?',
                       (proposal['subject'],now,app_id))
        for criterion in criteria:
            success_key='success:'+criterion['criterion']+':'+str(criterion['evidence']['source_message_id'])
            previous=db.execute('SELECT data FROM seller_application_events WHERE application_id=? AND event_key=?',(app_id,success_key)).fetchone()
            if previous:
                old=json.loads(previous['data'])
                if old['evidence']['source_text']==criterion['evidence']['source_text']:continue
            _event(db,app_id,success_key,
                   'success',criterion,now)
        return app_id


def applications(store,thread_id):
    with store.connection() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM seller_applications WHERE thread_id=? ORDER BY created_at,id',(thread_id,))]
        for row in rows:
            row['qualification']=json.loads(row['qualification'])
            row['criteria']=sorted({json.loads(e['data'])['criterion'] for e in db.execute(
                "SELECT data FROM seller_application_events WHERE application_id=? AND kind='success'",(row['id'],))})
            row['success']=bool(row['criteria']) and row['quality']=='valid'
    return rows


def manager_change(store,application_id,*,actor,manager,revision,action,reason):
    if action not in {'invalidate','close'} or not isinstance(reason,str) or not 1<=len(reason.strip())<=1000:
        raise ValueError('Нужна причина изменения заявки.')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        app=db.execute('SELECT * FROM seller_applications WHERE id=?',(application_id,)).fetchone()
        if not app:raise ValueError('Заявка не найдена.')
        thread=store._thread(db,app['thread_id']);_bound(thread)
        if actor!=thread['account_id'] or manager!=thread['manager_id']:raise PermissionError('Чужая заявка.')
        if app['revision']!=revision:raise StaleDialogue('Карточка заявки изменилась.')
        column,value=('quality','invalid') if action=='invalidate' else ('status','closed')
        db.execute(f'UPDATE seller_applications SET {column}=?,revision=revision+1,updated_at=? WHERE id=?',
            (value,int(store.clock()),application_id))
        _event(db,application_id,'manager:'+str(revision),action,dict(actor=actor,reason=reason.strip()),int(store.clock()))
        db.execute("UPDATE dialogue_threads SET revision=revision+1,control_epoch=control_epoch+1,resume_pending=0,"
                   "state=CASE WHEN state IN ('opt_out','closed') THEN state ELSE 'human' END,"
                   "mode=CASE WHEN mode='off' THEN mode ELSE 'copilot' END,control_reason=? WHERE id=?",
                   ('Менеджер изменил заявку. Проверьте диалог перед возвратом ИИ.',thread['id']))
        db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread['id'],))
