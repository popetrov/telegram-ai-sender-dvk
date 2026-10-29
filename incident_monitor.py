"""Durable metadata flight recorder; never reads texts or deletes Telegram data."""
import asyncio
import base64
from contextlib import closing
import hashlib
import io
import json
import os
import logging
import platform
from pathlib import Path
import sqlite3
import time
import uuid

ROOT = Path(__file__).resolve().parent
KINDS = {'startup', 'shutdown', 'heartbeat', 'send_attempt', 'send_result',
         'send_error', 'telegram_deleted', 'history_missing', 'baseline_missing',
         'request_blocked', 'update_failed', 'outgoing_seen', 'incoming_seen'}
KINDS.update({'runtime_error','connection','session_snapshot','monitor_warning'})
FIELDS={'operation','error_type','message_ids','peer_id','account_id','manager_id','version','files',
        'folder_id','pid','request_id','source','count','stage','reason_code','installation_id',
        'call_stack','local_sequence','machine_id','logger','level','sessions','connected','tls_ca_sha256'}


def validate_event(row):
    import re
    if (not isinstance(row,dict) or set(row)-FIELDS-{'kind','run_id','recorded_at'}
            or row.get('kind') not in KINDS or len(json.dumps(row))>32000):
        raise ValueError('Invalid monitor event')
    if not re.fullmatch('[0-9a-f]{32}',str(row.get('run_id',''))):
        raise ValueError('Invalid monitor run')
    if type(row.get('recorded_at')) is not int:
        raise ValueError('Invalid monitor time')
    for name in ('operation','error_type','stage','reason_code','source','manager_id'):
        if name in row and (not isinstance(row[name],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',row[name])):
            raise ValueError('Invalid monitor code')
    if 'message_ids' in row and (not isinstance(row['message_ids'],list) or len(row['message_ids'])>2000
                                  or any(type(mid) is not int or mid<=0 for mid in row['message_ids'])):
        raise ValueError('Invalid monitored message IDs')
    if 'files' in row and (not isinstance(row['files'],dict) or len(row['files'])>30
            or any(not re.fullmatch('[A-Za-z0-9_.-]{1,100}',name) or not isinstance(value,str)
                   or not re.fullmatch('[0-9a-f]{64}',value) for name,value in row['files'].items())):
        raise ValueError('Invalid monitor manifest')
    for field in ('folder_id','machine_id','tls_ca_sha256'):
        if field in row and (not isinstance(row[field],str) or not re.fullmatch('[0-9a-f]{64}',row[field])):
            raise ValueError('Invalid monitor fingerprint')
    for field in ('account_id','pid','count','local_sequence','level'):
        if field in row and (type(row[field]) is not int or row[field]<0):
            raise ValueError('Invalid monitor integer')
    if 'peer_id' in row and (type(row['peer_id']) is not int or row['peer_id']==0):
        raise ValueError('Invalid monitor peer')
    if 'connected' in row and type(row['connected']) is not bool:raise ValueError('Invalid connection state')
    for field in ('installation_id','request_id'):
        if field in row and (not isinstance(row[field],str) or not re.fullmatch('[0-9a-f]{32}',row[field])):
            raise ValueError('Invalid monitor identifier')
    if 'version' in row and (not isinstance(row['version'],str) or not re.fullmatch(r'\d+\.\d+\.\d+',row['version'])):
        raise ValueError('Invalid monitor version')
    if 'logger' in row and (not isinstance(row['logger'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',row['logger'])):
        raise ValueError('Invalid monitor logger')
    if 'call_stack' in row:
        if not isinstance(row['call_stack'],list) or len(row['call_stack'])>20:raise ValueError('Invalid monitor stack')
        for frame in row['call_stack']:
            if not isinstance(frame,dict) or set(frame)!={'file','line','function'}:raise ValueError('Invalid monitor frame')
            if not isinstance(frame['file'],str) or any(c in frame['file'] for c in '/\\:') or len(frame['file'])>150:
                raise ValueError('Invalid monitor filename')
            if type(frame['line']) is not int or not isinstance(frame['function'],str) or len(frame['function'])>150:
                raise ValueError('Invalid monitor frame')
    if 'sessions' in row:
        if not isinstance(row['sessions'],list) or len(row['sessions'])>100:raise ValueError('Invalid session snapshot')
        for session in row['sessions']:
            if (not isinstance(session,dict) or set(session)!={'fingerprint','current','created_at','last_active'}
                    or not re.fullmatch('[0-9a-f]{64}',str(session['fingerprint']))
                    or type(session['current']) is not bool
                    or any(type(session[f]) is not int for f in ('created_at','last_active'))):
                raise ValueError('Invalid session metadata')
    return row


class Recorder:
    def __init__(self, path):
        self.path = Path(path)
        self.run_id = uuid.uuid4().hex

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('Unsafe monitor journal')
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS monitor_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS monitor_messages (
            message_id INTEGER PRIMARY KEY, peer_id INTEGER, seen INTEGER NOT NULL,
            misses INTEGER NOT NULL DEFAULT 0, checked_at INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS monitor_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS monitor_pending ON monitor_events(acknowledged,sequence);''')
        return db

    def record(self, kind, **fields):
        if kind not in KINDS:
            raise ValueError('Unknown monitor event')
        allowed = FIELDS
        if set(fields) - allowed:
            raise ValueError('Unexpected monitor fields')
        data = dict(kind=kind, run_id=self.run_id, recorded_at=int(time.time()), **fields)
        # A programmer cannot accidentally place raw exception text or credentials here.
        for name in ('operation', 'error_type', 'stage', 'reason_code', 'source'):
            if name in data:
                import re
                if not isinstance(data[name], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', data[name]):
                    raise ValueError('Unsafe monitor code')
        if len(json.dumps(data)) > 32000:
            raise ValueError('Monitor event too large')
        validate_event(data)
        with closing(self.connect()) as db, db:
            event_id = uuid.uuid4().hex
            db.execute('INSERT INTO monitor_events(event_id,data) VALUES (?,?)',
                       (event_id, json.dumps(data, separators=(',', ':'))))
        return event_id

    def identity(self):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO monitor_settings VALUES (?,?)',
                       ('installation', uuid.uuid4().hex))
            return db.execute('SELECT value FROM monitor_settings WHERE key=?', ('installation',)).fetchone()[0]

    def bind(self, account_id, manager_id):
        subject=str(manager_id)+':'+str(account_id)
        with closing(self.connect()) as db, db:
            row=db.execute("SELECT value FROM monitor_settings WHERE key='subject'").fetchone()
            if row and row[0]!=subject:
                raise ValueError('Monitor journal belongs to another Telegram identity')
            db.execute("INSERT OR IGNORE INTO monitor_settings VALUES ('subject',?)",(subject,))

    def observe(self, ids, peer_id=None):
        with closing(self.connect()) as db, db:
            for mid in ids:
                if type(mid) is int and mid > 0:
                    db.execute('INSERT INTO monitor_messages(message_id,peer_id,seen) VALUES (?,?,1) '
                               'ON CONFLICT(message_id) DO UPDATE SET seen=1,misses=0,peer_id=COALESCE(excluded.peer_id,peer_id)',
                               (mid, peer_id))

    def seed(self, database):
        """Old IDs are unverified baseline, never asserted to have disappeared now."""
        if not Path(database).exists():
            return
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)) as source:
            try:
                ids = [r[0] for r in source.execute('SELECT telegram_message_id FROM messages '
                       'WHERE telegram_message_id IS NOT NULL AND status=\'sent\'')]
            except sqlite3.OperationalError:
                return
        with closing(self.connect()) as db, db:
            db.executemany('INSERT OR IGNORE INTO monitor_messages(message_id,seen) VALUES (?,0)', ((i,) for i in ids))

    def apply_check(self, ids, present):
        missing = []
        historical = []
        with closing(self.connect()) as db, db:
            for mid in ids:
                row = db.execute('SELECT * FROM monitor_messages WHERE message_id=?', (mid,)).fetchone()
                if row is None:
                    continue
                if mid in present:
                    db.execute('UPDATE monitor_messages SET seen=1,misses=0,checked_at=? WHERE message_id=?', (time.time(),mid))
                else:
                    misses = row['misses']+1
                    db.execute('UPDATE monitor_messages SET misses=?,checked_at=? WHERE message_id=?', (misses,time.time(),mid))
                    if row['seen'] and misses == 2:
                        missing.append(mid)
                    elif not row['seen'] and misses == 1:
                        historical.append(mid)
        if historical:
            self.record('baseline_missing', message_ids=historical, source='local_sent_history')
        if missing:
            self.record('history_missing', message_ids=missing, source='two_successful_checks')
        return missing

    def pending(self):
        rows=self.pending_batch(1)
        return rows[0] if rows else None

    def pending_batch(self, limit=20):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM monitor_events WHERE acknowledged=0 ORDER BY "
                              "CASE WHEN json_extract(data,'$.kind') IN ('telegram_deleted','history_missing','request_blocked') "
                              'THEN 0 ELSE 1 END,sequence LIMIT ?', (limit,)).fetchall()

    def acknowledge(self, event_id):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE monitor_events SET acknowledged=1 WHERE event_id=?', (event_id,))


_default = Recorder(ROOT/'data/incident-monitor.sqlite')


def record(kind, **fields):
    # Failure of mandatory audit prevents a new application send, rather than hiding it.
    try:
        return _default.record(kind, **fields)
    except Exception:
        from telegram_safety import default_journal, SafetyStop
        default_journal().block('observed_monitor_unavailable')
        raise SafetyStop('Журнал контроля недоступен. Отправки остановлены до проверки.')


def startup(account_id):
    _default.bind(account_id,os.getenv('MANAGER_ID',''))
    files = {}
    for name in ('safe_telegram.py','access_runtime.py','main.py','startup_update.py','bot.py',
                 'incident_monitor.py','telegram_client.py','telegram_safety.py','app_profile_client.py',
                 'sync_runtime.py','config.py','dialogue_runtime.py'):
        path = ROOT/name
        if path.exists():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    version = (ROOT/'version.txt').read_text('utf-8-sig').strip()
    machine=platform.node()
    if os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Cryptography') as key:
                machine+=str(winreg.QueryValueEx(key,'MachineGuid')[0])
        except OSError:
            pass
    record('startup', account_id=account_id, manager_id=os.getenv('MANAGER_ID',''),
           version=version, files=files, pid=os.getpid(), installation_id=_default.identity(),
           folder_id=hashlib.sha256(str(ROOT).encode()).hexdigest(),
           machine_id=hashlib.sha256(('DVK-MONITOR-1:'+machine).encode()).hexdigest())
    report=ROOT/'data/update-network-report.json'
    if report.is_file():
        try:
            data=json.loads(report.read_text('utf-8'))
            record('update_failed',error_type=data['error_type'],stage=data['stage'])
        except (ValueError,KeyError,OSError):
            pass
    launcher=Path(os.environ.get('DVK_LAUNCHER_ROOT',str(ROOT)))
    for candidate in dict.fromkeys((ROOT/'DVK_START_REPORT.txt',launcher/'DVK_START_REPORT.txt')):
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size>64000:continue
        try:
            failure=json.loads(candidate.read_text('utf-8'))
            if failure.get('protocol')=='DVK_START_FAILURE_V1':
                record('update_failed',error_type=failure['error_type'],stage=failure['stage'],reason_code=failure['reason_code'])
        except (ValueError,KeyError,OSError):pass


async def flush(client):
    from access_runtime import transport_lock
    from access_lease import canonical
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from sync_protocol import encrypt, event_digest, verify_receipt
    rows = _default.pending_batch()
    if not rows:
        return False
    authority = os.environ['ACCESS_BOT_USERNAME'].lstrip('@')
    me = await client.get_me()
    subject = os.environ['MANAGER_ID'].strip()+':'+str(me.id)
    installation=_default.identity()
    members=[]
    for row in rows:
        event=json.loads(row['data'])
        event['local_sequence']=row['sequence']
        candidate=dict(event_id=row['event_id'],data=dict(table='monitor_events',row=event))
        if members and len(canonical(members+[candidate]))>64*1024:break
        members.append(candidate)
    if len(members)==1:
        data=dict(installation=installation,data=members[0]['data'])
        event_id=members[0]['event_id']
    else:
        data=dict(installation=installation,data=dict(table='sync_batch',row=dict(events=members)))
        event_id=event_digest(data)[:32]
    packet = encrypt(os.environ['SYNC_PUBLIC_KEY'],dict(subject=subject,event_id=event_id,data=data),compress=True)
    stream = io.BytesIO(packet)
    stream.name='encrypted.dvk'
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'],validate=True))
    entity=await client.get_entity(authority)
    if not getattr(entity,'bot',False) or (entity.username or '').casefold()!=authority.casefold():
        raise ValueError('Invalid monitor receiver')
    client._monitor_service_id=entity.id
    from safe_telegram import _report_permission
    async with transport_lock:
        async with client.conversation(entity, timeout=20, exclusive=True) as conversation:
            token=_report_permission.set((entity.id,'DVK_SYNC_V1'))
            try:
                sent = await client.send_file(entity,stream,caption='DVK_SYNC_V1',force_document=True,silent=True)
            finally:
                _report_permission.reset(token)
            async with asyncio.timeout(20):
                while True:
                    response = await conversation.get_response(sent)
                    text = response.raw_text or ''
                    if text.startswith('DVK_RECEIPT '):
                        envelope=json.loads(text[12:])
                        # A late receipt for another packet is not an acknowledgement.
                        if envelope.get('payload',{}).get('event_id')!=event_id:continue
                        verify_receipt(key,envelope,subject,event_id,event_digest(data))
                        for member in members:_default.acknowledge(member['event_id'])
                        return True


class ErrorRecorder(logging.Handler):
    """Only structured exception types/frames; log message strings are never exported."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.last={}

    def emit(self, event):
        try:
            import re
            logger=event.name if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',event.name) else 'other'
            error_type=event.exc_info[0].__name__ if event.exc_info else 'LogWarning'
            key=(logger,event.levelno,error_type)
            now=time.monotonic()
            if now-self.last.get(key,-1000)<60:return
            self.last[key]=now
            fields=dict(logger=logger,level=event.levelno,error_type=error_type,source='application_logger')
            if event.exc_info:
                import traceback
                fields['call_stack']=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
                                      for f in traceback.extract_tb(event.exc_info[2],limit=16)]
            record('runtime_error',**fields)
        except Exception:
            # record() already latches a disk failure; never recurse into logging.
            pass


async def run(client, notify):
    from telethon import events
    from telegram_safety import default_journal
    async def stop_observed(operation):
        incident = default_journal().block(operation)
        if not incident['manager_notified']:
            try:
                await notify('Обнаружено исчезновение сообщений Telegram. Отправки и ИИ остановлены. '
                             'Причина удаления пока неизвестна; это не подтверждение, что удалила программа. '
                             'Журнал сохранён для центра. Код: '+incident['incident_id'])
                default_journal().delivered(incident['incident_id'],'manager_notified')
            except Exception:pass

    async def deleted(event):
        if getattr(event,'is_channel',False):
            return
        ids = list(event.deleted_ids)
        with closing(_default.connect()) as db:
            tracked=[mid for mid in ids if db.execute('SELECT 1 FROM monitor_messages WHERE message_id=?',(mid,)).fetchone()]
        record('telegram_deleted',message_ids=ids[:2000],count=len(ids),
               source='tracked_private_history' if tracked else 'unattributed_update')
        if tracked:
            await stop_observed('observed_telegram_deletion')

    async def message(event):
        msg = event.message
        if not event.is_private or getattr(msg,'sender',None) and getattr(msg.sender,'bot',False):
            return
        # Service bot traffic is excluded by bot identity, never by user text.
        peer = await event.get_chat()
        if getattr(peer,'bot',False):
            return
        record('outgoing_seen' if msg.out else 'incoming_seen',message_ids=[msg.id],peer_id=event.chat_id,source='telegram_update')
        _default.observe([msg.id],event.chat_id)

    client.add_event_handler(deleted,events.MessageDeleted())
    client.add_event_handler(message,events.NewMessage())
    import config
    _default.seed(ROOT/config.DATABASE_PATH)
    handler=ErrorRecorder()
    logging.getLogger().addHandler(handler)
    loop=asyncio.get_running_loop()
    previous_exception_handler=loop.get_exception_handler()
    def on_exception(loop,context):
        exc=context.get('exception')
        try:record('runtime_error',error_type=type(exc).__name__ if exc else 'UnhandledTask',source='asyncio')
        finally:
            if previous_exception_handler:previous_exception_handler(loop,context)
            else:loop.default_exception_handler(context)
    loop.set_exception_handler(on_exception)
    heartbeat_at=0
    check_at=0
    sessions_at=0
    connected=None
    try:
        while True:
            now=time.monotonic()
            current=bool(client.is_connected())
            if current!=connected:
                record('connection',connected=current)
                connected=current
            if now>=heartbeat_at:
                record('heartbeat',connected=current)
                heartbeat_at=now+300
            if now>=sessions_at and current:
                sessions_at=now+900
                try:
                    from telethon.tl.functions.account import GetAuthorizationsRequest
                    result=await client(GetAuthorizationsRequest())
                    sessions=[dict(fingerprint=hashlib.sha256(str(a.hash).encode()).hexdigest(),
                                   current=bool(a.current),created_at=int(a.date_created.timestamp()),
                                   last_active=int(a.date_active.timestamp())) for a in result.authorizations[:100]]
                    record('session_snapshot',sessions=sessions,count=len(result.authorizations))
                except Exception as exc:
                    record('runtime_error',error_type=type(exc).__name__,stage='read_authorizations')
            if now>=check_at:
                check_at=now+120
                try:
                    with closing(_default.connect()) as db:
                        ids=[r[0] for r in db.execute('SELECT message_id FROM monitor_messages WHERE misses<2 ORDER BY checked_at,message_id LIMIT 50')]
                    if ids:
                        found=await client.get_messages(None,ids=ids)
                        present={m.id for m in found if m and getattr(m,'message',None) is not None}
                        if _default.apply_check(ids,present):
                            await stop_observed('observed_history_missing')
                except Exception as exc:
                    record('runtime_error',error_type=type(exc).__name__,stage='history_check')
                    print('Проверка истории отложена ('+type(exc).__name__+').',flush=True)
            try:
                await flush(client)
            except Exception as exc:
                print('Журнал контроля ожидает центр ('+type(exc).__name__+').',flush=True)
                await asyncio.sleep(30)
            await asyncio.sleep(2)
    finally:
        logging.getLogger().removeHandler(handler)
        if loop.get_exception_handler() is on_exception:loop.set_exception_handler(previous_exception_handler)
        client.remove_event_handler(deleted)
        client.remove_event_handler(message)


def center_notice(store, notices, account_id, event_id, data):
    if data.get('data',{}).get('table')=='sync_batch':
        from sync_protocol import batch_members
        for member_id,member in batch_members(data):center_notice(store,notices,account_id,member_id,member)
        return
    row=data.get('data',{}).get('row',{})
    if data.get('data',{}).get('table')=='monitor_events' and row.get('kind')=='runtime_error':
        if row.get('stage')=='monitor_task_failed':return  # durable safety notification covers this stop
        if row.get('level',0)<logging.ERROR and row.get('source')!='asyncio':return
        from owner_notifications import manager_label
        error=row.get('error_type','UnknownError')
        key='monitor-error:'+str(account_id)+':'+error+':'+row.get('logger',row.get('source','runtime'))+':'+str(int(time.time())//3600)
        notices.once(key,'Сбой локальной программы. Менеджер: '+manager_label(store,account_id)+
                     '. Тип: '+error+'. Техническое событие сохранено в журнале. Код: '+event_id)
        return
    if data.get('data',{}).get('table')!='monitor_events' or row.get('kind') not in ('telegram_deleted','history_missing','request_blocked'):
        return
    if row.get('kind')=='telegram_deleted' and row.get('source')!='tracked_private_history':
        return
    from owner_notifications import manager_label
    label=manager_label(store,account_id)
    notices.once('monitor:'+str(account_id)+':'+event_id,
                 'Контроль переписки: '+label+'. '+
                 ('Опасный запрос заблокирован программой.' if row['kind']=='request_blocked' else
                  'Получен сигнал об удалении или исчезновении сообщений. Кто удалил — пока неизвестно.')+
                 ' Требуется проверка журнала. Код события: '+event_id)


def validate_bound_event(data,account_id,subject):
    if data.get('data',{}).get('table')!='monitor_events':return
    event=validate_event(data['data'].get('row'))
    if 'account_id' in event and event['account_id']!=account_id:raise ValueError('Monitor account mismatch')
    if 'manager_id' in event and event['manager_id']+':'+str(account_id)!=subject:
        raise ValueError('Monitor manager mismatch')


def export_report(store,account_id,limit=20000):
    with store.connection() as db:
        manager=db.execute('SELECT subject,company_id FROM managers WHERE account_id=?',(account_id,)).fetchone()
        if not manager or not manager['company_id']:raise ValueError('Менеджер не подключён к компании')
        rows=db.execute("SELECT event_id,received_at,payload,subject,company_id FROM events WHERE account_id=? "
                        "AND json_extract(payload,'$.data.table')='monitor_events' "
                        'ORDER BY received_at DESC,rowid DESC LIMIT ?', (account_id,limit)).fetchall()
    events=[]
    for row in rows:
        data=json.loads(row['payload'])
        try:validate_bound_event(data,account_id,row['subject'])
        except (ValueError,TypeError,KeyError):continue
        events.append(dict(event_id=row['event_id'],received_at=row['received_at'],
                           installation=data['installation'],company_id=row['company_id'],event=data['data']['row']))
    from owner_notifications import manager_label
    return dict(protocol='DVK_MONITOR_EXPORT_V1',generated_at=int(time.time()),account_id=account_id,
                manager=manager_label(store,account_id),subject=manager['subject'],company_id=manager['company_id'],
                events=list(reversed(events)),limit=limit,possibly_truncated=len(rows)==limit,
                limits='Metadata only. Telegram does not identify who deleted a message. '
                       'No events means no evidence, not proof of normal operation. '
                       'Events before module installation cannot be reconstructed.')


def register_admin(application, store, admin_id):
    from telegram.ext import CommandHandler, CallbackQueryHandler
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup

    async def overview(message,page=0):
        managers=store.list_managers()
        managers=[m for m in managers if m['company_id']]
        from owner_notifications import manager_label
        buttons=[[InlineKeyboardButton(manager_label(store,m['account_id']),callback_data='monitor:view:'+str(m['account_id']))]
                 for m in managers[page*20:(page+1)*20]]
        if page>0:buttons.append([InlineKeyboardButton('Назад',callback_data='monitor:page:'+str(page-1))])
        if len(managers)>(page+1)*20:buttons.append([InlineKeyboardButton('Дальше',callback_data='monitor:page:'+str(page+1))])
        await message.reply_text('Контроль переписки. Выберите менеджера.\n'
            'Журнал появляется после установки модуля на его компьютере.',reply_markup=InlineKeyboardMarkup(buttons))

    async def show(message,account):
        try:report=export_report(store,account,200)
        except ValueError:
            await message.reply_text('Менеджер не найден или не подключён.');return
        events=report['events']
        if not events:
            await message.reply_text('Журнал контроля от этого менеджера ещё не получен. '
                'Это не означает отсутствия ошибок: модуль может быть не установлен или связь недоступна.')
            return
        from datetime import datetime
        from zoneinfo import ZoneInfo
        counts={}
        for r in events:
            kind=r['event']['kind'];counts[kind]=counts.get(kind,0)+1
        latest=datetime.fromtimestamp(events[-1]['received_at'],ZoneInfo('Asia/Dubai')).strftime('%d.%m %H:%M:%S')
        start=next((r['event'] for r in reversed(events) if r['event']['kind']=='startup'),None)
        text='Контроль: '+report['manager']+'\nПоследний приём: '+latest+' (Дубай).'
        if start:text+='\nВерсия: '+start.get('version','не указана')+' · запуск '+start['run_id'][:8]
        text+='\nВ последних '+str(len(events))+' событиях:\nПопытки отправки: '+str(counts.get('send_attempt',0))
        text+='\nОтветы Telegram об отправке: '+str(counts.get('send_result',0))
        text+='\nОшибки отправки: '+str(counts.get('send_error',0))
        text+='\nСигналы удаления: '+str(counts.get('telegram_deleted',0)+counts.get('history_missing',0))
        text+='\nЗаблокированные запросы: '+str(counts.get('request_blocked',0))
        text+='\nОшибки программы: '+str(counts.get('runtime_error',0))
        text+='\nЭто события журнала, не полный итог рассылки.'
        await message.reply_text(text,reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('📄 Выгрузить журнал',callback_data='monitor:export:'+str(account))],
            [InlineKeyboardButton('🔄 Обновить',callback_data='monitor:view:'+str(account))]]))

    async def download(message,account):
        try:report=export_report(store,account)
        except ValueError:
            await message.reply_text('Менеджер не найден или не подключён.');return
        raw=json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8')
        stream=io.BytesIO(raw);stream.name='DVK_MONITOR_'+str(account)+'.json'
        await message.reply_document(document=stream,caption='Журнал событий без текстов переписки и ключей. '
            'Его можно передать для расследования.')
    async def status(update,context):
        if update.effective_user.id!=admin_id or update.effective_chat.type!='private':
            return
        if not context.args:
            await overview(update.effective_message)
            return
        if len(context.args)!=1 or not context.args[0].isdigit():
            await update.effective_message.reply_text('Проверка журнала: /monitor TELEGRAM_ID');return
        account=int(context.args[0])
        if (update.effective_message.text or '').split()[0].split('@')[0]=='/monitor_export':
            await download(update.effective_message,account)
        else:await show(update.effective_message,account)

    async def callback(update,context):
        if update.effective_user.id!=admin_id or update.effective_chat.type!='private':return
        query=update.callback_query
        await query.answer()
        parts=(query.data or '').split(':')
        if len(parts)!=3 or not parts[2].isdigit():return
        value=int(parts[2])
        if parts[1]=='page':await overview(query.message,min(value,1000))
        elif parts[1]=='view':await show(query.message,value)
        elif parts[1]=='export':await download(query.message,value)
    application.add_handler(CommandHandler('monitor',status))
    application.add_handler(CommandHandler('monitor_export',status))
    application.add_handler(CallbackQueryHandler(callback,pattern=r'^monitor:(view|export|page):\d+$'),group=-4)
