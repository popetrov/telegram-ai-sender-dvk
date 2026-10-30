"""Strict metadata provenance. This module has no Telegram side effects."""
import asyncio,contextlib,contextvars,hashlib,json,re,time,traceback,uuid
from pathlib import Path

_origin=contextvars.ContextVar('dvk_operation_origin',default=None)
SOURCES={'campaign','auto_reply','warmup','service_sync','access_check','update','service','unknown'}
FIELDS={'origin_key','from_peer_id','to_peer_id','from_peer_type','to_peer_type','application_fingerprint','op_id','parent_id','trigger','origin_table','origin_id','account_id','peer_id','peer_type',
        'target_ids','target_count','target_digest','constructor','flags','part','parts','monotonic_ns',
        'session_fingerprint','data_store_id','runtime_id','parent_pid','python_version','sdk_version',
        'task_name','wire_message_id','capture_mode','last_seen_at','observed_at','peer_verified',
        'pts','pts_count','coverage','schema_version','received_at','error_type'}
HEX={'origin_key':32,'application_fingerprint':64,'op_id':32,'parent_id':32,'session_fingerprint':64,'data_store_id':32,'runtime_id':32,'target_digest':64}
NUM={'origin_id','account_id','target_count','constructor','part','parts','monotonic_ns','parent_pid',
     'wire_message_id','last_seen_at','observed_at','pts','pts_count','schema_version','received_at'}
FLAGS={'revoke','just_clear','max_id','min_id','min_date','max_date','period','ttl_seconds','ttl_period'}

def validate(value):
    if not isinstance(value,dict) or set(value)-FIELDS or len(json.dumps(value))>16000:raise ValueError('Invalid forensic metadata')
    for k,v in value.items():
        if k in HEX:
            if not isinstance(v,str) or not re.fullmatch('[0-9a-f]{'+str(HEX[k])+'}',v):raise ValueError('Invalid forensic fingerprint')
        elif k in NUM:
            if type(v) is not int:raise ValueError('Invalid forensic number')
        elif k in ('peer_id','from_peer_id','to_peer_id'):
            if type(v) is not int or not v:raise ValueError('Invalid forensic peer')
        elif k=='target_ids':
            if not isinstance(v,list) or len(v)>200 or any(type(i) is not int or i<=0 for i in v):raise ValueError('Invalid forensic IDs')
        elif k=='flags':
            if not isinstance(v,dict) or set(v)-FLAGS or any(type(x) not in (bool,int) for x in v.values()):raise ValueError('Invalid forensic flags')
        elif k=='peer_verified':
            if type(v) is not bool:raise ValueError('Invalid forensic verification')
        elif not isinstance(v,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',v):raise ValueError('Unsafe forensic text')
    if 'trigger' in value and value['trigger'] not in SOURCES:raise ValueError('Unknown forensic source')
    return value

@contextlib.contextmanager
def origin(trigger,table=None,row_id=None):
    data={'trigger':trigger}
    if table is not None:data['origin_table']=table
    if row_id is not None:data['origin_id' if type(row_id) is int else 'origin_key']=row_id
    validate(data);token=_origin.set(data)
    try:yield
    finally:_origin.reset(token)

def source():
    if _origin.get():return dict(_origin.get())
    names={Path(f.filename).name for f in traceback.extract_stack(limit=24)}
    if 'dialogue_calendar.py' in names:return {'trigger':'warmup'}
    if 'dialogue_runtime.py' in names:return {'trigger':'auto_reply'}
    if 'sync_runtime.py' in names or 'incident_monitor.py' in names:return {'trigger':'service_sync'}
    if 'access_runtime.py' in names or 'app_profile_client.py' in names:return {'trigger':'access_check'}
    if 'startup_update.py' in names or 'unified_install.py' in names:return {'trigger':'update'}
    if 'bot.py' in names:return {'trigger':'campaign'}
    return {'trigger':'unknown'}

def targets(request):
    from safe_telegram import requests_in
    ids=[];flags={};peer=None;forwarded={}
    for item in requests_in(request):
        for slot in ('from_peer','to_peer'):
            value=getattr(item,slot,None)
            for field,label in [('user_id','user'),('chat_id','chat'),('channel_id','channel')]:
                pid=getattr(value,field,None)
                if type(pid) is int and pid:
                    # Stored separately: forwarding IDs belong to the source,
                    # not to the destination dialog.
                    forwarded[slot+'_id']=pid;forwarded[slot+'_type']=label
        p=getattr(item,'peer',None)
        for field,label in [('user_id','user'),('chat_id','chat'),('channel_id','channel')]:
            v=getattr(p,field,None)
            if type(v) is int and v:peer=(v,label)
        for k in FLAGS:
            v=getattr(item,k,None)
            if type(v) in (bool,int):flags[k]=v
            elif k in ('min_date','max_date') and hasattr(v,'timestamp'):flags[k]=int(v.timestamp())
        v=getattr(item,'id',None)
        if isinstance(v,(list,tuple)):
            ids.extend(getattr(i,'id',i) for i in v if type(getattr(i,'id',i)) is int and getattr(i,'id',i)>0)
    data={'flags':flags,'target_count':len(ids),'target_digest':hashlib.sha256(json.dumps(ids,separators=(',',':')).encode()).hexdigest()}
    data.update(forwarded)
    if peer:data.update(peer_id=peer[0],peer_type=peer[1])
    return data,ids

def emit(client,request,stage,op_id=None,**extra):
    from incident_monitor import record
    data,ids=targets(request)
    provenance=getattr(request,'_dvk_origin',None)
    if provenance is None:
        provenance=source()
        try:request._dvk_origin=dict(provenance)
        except (AttributeError,TypeError):pass
    data.update(provenance);data.update(extra)
    data['op_id']=op_id or getattr(request,'_dvk_operation_id',None) or uuid.uuid4().hex
    try:request._dvk_operation_id=data['op_id']
    except (AttributeError,TypeError):pass
    account=getattr(client,'_self_id',None)
    if type(account) is int and account>0:data['account_id']=account
    data['monotonic_ns']=time.monotonic_ns()
    constructor=getattr(request,'CONSTRUCTOR_ID',None)
    if type(constructor) is int:data['constructor']=constructor
    try:
        task=asyncio.current_task()
        name=task.get_name() if task else ''
        if re.fullmatch(r'Task-[0-9]+|incident-monitor|central-access|owner-data-sync|telegram-safety-notifications|dialogue-[0-9a-f-]{1,50}',name):data['task_name']=name
    except RuntimeError:pass
    frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_stack(limit=18)]
    chunks=[ids[i:i+200] for i in range(0,len(ids),200)] or [[]]
    for part,chunk in enumerate(chunks):
        member=dict(data,target_ids=chunk,part=part,parts=len(chunks));validate(member)
        record('forensic_operation',operation=type(request).__name__,stage=stage,evidence=member,call_stack=frames if part==0 else [])
    return data['op_id']

def validate_chain(row):
    if 'event_hash' not in row:return
    if any(not isinstance(row.get(k),str) or not re.fullmatch('[0-9a-f]{64}',row[k]) for k in ('previous_hash','event_hash')):raise ValueError('Invalid event hash')
    body={k:v for k,v in row.items() if k!='event_hash'}
    expected=hashlib.sha256(bytes.fromhex(row['previous_hash'])+json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    if expected!=row['event_hash']:raise ValueError('Forensic event hash mismatch')
