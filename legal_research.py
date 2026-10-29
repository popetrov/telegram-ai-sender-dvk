"""Verified legal facts, immutable attempts and resumable preparation. Never sends messages."""
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRODUCTS={'Право_ООО','Право_ИП'}
PIPELINE_REVISION=1
GREETINGS=('Здравствуйте!','Добрый день!')
OFFERS=(
    'Помогаем защищать интересы в арбитражных спорах. Готовы обсудить юридическую помощь по этому делу?',
    'Занимаемся сопровождением арбитражных споров. С кем можно обсудить юридическую помощь по этому делу?',
    'Если по этому спору нужна юридическая поддержка, можем обсудить задачу. Будет удобно?')
STYLE_SCHEMA=dict(type='object',properties=dict(greeting=dict(type='string',enum=list(GREETINGS)),
    offer_index=dict(type='integer',enum=[0,1,2])),required=['greeting','offer_index'],additionalProperties=False)
_locks={}
_locks_guard=threading.Lock()
_writer_cooldowns={}


class ResearchProblem(RuntimeError):
    def __init__(self,reason,status='manual_review',code='facts'):
        super().__init__(reason)
        self.status,self.code=status,code


def normalized(value):
    return re.sub(r'[\W_]+',' ',str(value).casefold().replace('ё','е')).strip()


def money(value):
    try:
        amount=Decimal(str(value).replace(' ','').replace('\u00a0','').replace(',','.'))
        if not amount.is_finite() or amount<=0 or amount!=amount.quantize(Decimal('.01')):raise ValueError()
        return amount
    except (InvalidOperation,ValueError):raise ResearchProblem('Не подтверждена точная положительная сумма требований.')


def _safe_text(value,label):
    if not isinstance(value,str) or not value.strip() or len(value)>600 or any(ord(c)<32 and c not in '\n\t\r' for c in value):
        raise ResearchProblem('В карточке отсутствует или повреждено поле: '+label+'.')
    return ' '.join(value.split())


def verified_facts(result,inn):
    """A deterministic gate, independent of the writer or its web-search availability."""
    from arbitration_api import _parse_date
    from datetime import datetime
    if result.get('status')=='ERROR':
        code=result.get('error_code','parser')
        status='research_paused' if code in ('quota','credentials','rate_limit') else 'research_failed'
        if code in ('identity','verification'):status='manual_review'
        raise ResearchProblem(result.get('reason') or 'Судебный поиск не завершён.',status,code)
    if result.get('status')!='CONFIRMED':
        raise ResearchProblem('У парсера нет подтверждённого дела ответчика. Общее письмо вместо судебного повода не формируется.')
    case=result.get('case') or {}
    if case.get('defendant_inn')!=inn:raise ResearchProblem('ИНН ответчика не совпал с исследуемым ИНН.')
    number=_safe_text(case.get('case_number'),'номер дела').upper().replace('–','-')
    defendant=_safe_text(case.get('defendant_name'),'ответчик')
    plaintiffs=case.get('plaintiffs')
    if not isinstance(plaintiffs,list) or not plaintiffs:raise ResearchProblem('Не установлены истцы выбранного дела.')
    plaintiffs=[_safe_text(p,'истец') for p in plaintiffs]
    respondents=case.get('respondents') or []
    if len({normalized(p) for p in respondents})!=1:
        raise ResearchProblem('В деле несколько ответчиков или их состав не подтверждён. Нельзя приписать общую сумму одному клиенту.')
    court=_safe_text(case.get('court'),'суд')
    if _parse_date(case.get('start_date'))==datetime.min:raise ResearchProblem('Не подтверждена дата регистрации выбранного дела.')
    event=case.get('latest_event')
    if not isinstance(event,dict) or _parse_date(event.get('date'))==datetime.min:
        raise ResearchProblem('В карточке отсутствует датированное процессуальное событие.')
    content=_safe_text(event.get('content') or event.get('type'),'содержание события')
    # ClaimSum must be backed by a claim/demand event, never a fee or generic motion.
    claims=[]
    for source in case.get('events') or []:
        title=normalized(str(source.get('content') or '')+' '+str(source.get('type') or ''))
        if not any(word in title for word in ('исков','требован','выдаче судебного приказа','взыскании')):continue
        if any(word in title for word in ('госпошлин','судебных расходов')):continue
        if 'встречн' in title or str(source.get('declarer_inn') or '')==inn:continue
        try:amount=money(source.get('claim_sum'))
        except ResearchProblem:continue
        if _parse_date(source.get('date'))!=datetime.min:claims.append((source,amount))
    if not claims:raise ResearchProblem('Не найдена сумма требований с источником в исковом заявлении или уточнении требований.')
    claims.sort(key=lambda item:_parse_date(item[0]['date']),reverse=True)
    claim,amount=claims[0]
    facts=dict(case_number=number,case_id=_safe_text(case.get('case_id'),'ID дела'),
        defendant_inn=inn,defendant=defendant,plaintiffs=plaintiffs,court=court,
        start_date=case['start_date'],claim_sum=format(amount,'.2f'),
        claim_source=claim,latest_event=event,finished=case.get('finished'),state=case.get('state'),
        claim_date=_parse_date(claim['date']).strftime('%d.%m.%Y'),
        event_date=_parse_date(event['date']).strftime('%d.%m.%Y'),event_content=content,
        card_url=case.get('card_url'))
    return facts


def fallback_message(facts,sender,greeting=GREETINGS[0],offer=OFFERS[0]):
    amount=f"{money(facts['claim_sum']):,.2f}".replace(',',' ').replace('.',',')
    return (f"{greeting} Меня зовут {sender}, ООО «ДВК Право».\n\n"
        f"В карточке дела № {facts['case_number']} ({facts['court']}) отражено событие «{facts['event_content']}» "
        f"от {facts['event_date']}. Ответчик — {facts['defendant']}; "
        f"{'истцы' if len(facts['plaintiffs'])>1 else 'истец'} — {'; '.join(facts['plaintiffs'])}. "
        f"Сумма требований по заявлению от {facts['claim_date']} — {amount} руб.\n\n"
        + offer)


def validate_message(text,facts,sender):
    errors=[]
    clean=normalized(text)
    for label,value in [('номер дела',facts['case_number']),('ответчик',facts['defendant']),('суд',facts['court']),
                        ('имя отправителя',sender),('событие',facts['event_content'])]+[('истец',p) for p in facts['plaintiffs']]:
        if normalized(value) not in clean:errors.append('Отсутствует или изменено поле: '+label)
    if facts['event_date'] not in text:errors.append('Не указана точная дата события')
    numbers=re.findall(r'[АA]\d{1,3}[-–]\d+/\d{2,4}',text)
    canon=lambda v:v.replace('A','А').replace('–','-')
    if not numbers or any(canon(n)!=canon(facts['case_number']) for n in numbers):errors.append('Подставлен другой номер дела')
    amounts=re.findall(r'(?<!\d)(\d[\d \u00a0]*(?:[.,]\d{1,2})?)\s*(?:руб\b|рублей\b|рубля\b|₽)',text,re.I)
    try:
        if not amounts or any(money(a)!=money(facts['claim_sum']) for a in amounts):errors.append('Сумма требований отсутствует или изменена')
    except ResearchProblem:errors.append('Неверная денежная сумма')
    if any(p in clean for p in ('гарантируем выигрыш','выиграем дело','обязаны выплатить','проиграли дело')):
        errors.append('Неподтверждённый исход спора или обещание результата')
    # A filing source must never be promoted to an acceptance/hearing/outcome.
    if 'исковое заявление' in normalized(facts['event_content']):
        if any(p in clean for p in ('принят к производству','иск принят','назначено заседание','суд решил','суд взыскал')):
            errors.append('Поступление заявления подменено другим процессуальным событием')
    if len(text)>3500 or 'КОНЕЦ СООБЩЕНИЯ' in text:errors.append('Неверный формат готового сообщения')
    return errors


class Journal:
    def __init__(self,manager):
        import config
        self.path=Path(config.DATABASE_PATH)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.manager=manager
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS legal_research_jobs(
                job_key TEXT PRIMARY KEY,manager TEXT NOT NULL,row_json TEXT NOT NULL,state TEXT NOT NULL,
                updated_at INTEGER NOT NULL,generated_json TEXT);
                CREATE TABLE IF NOT EXISTS legal_research_attempts(
                id TEXT PRIMARY KEY,job_key TEXT NOT NULL,created_at INTEGER NOT NULL,state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',facts_json TEXT,model_json TEXT);
                CREATE TABLE IF NOT EXISTS legal_research_requests(
                id INTEGER PRIMARY KEY,attempt_id TEXT NOT NULL,created_at INTEGER NOT NULL,event_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legal_service_notices(
                notice_id TEXT PRIMARY KEY,manager TEXT NOT NULL,code TEXT NOT NULL,created_at INTEGER NOT NULL,sent_at INTEGER);
                CREATE INDEX IF NOT EXISTS legal_jobs_retry ON legal_research_jobs(manager,state,updated_at);
                CREATE INDEX IF NOT EXISTS legal_requests_attempt ON legal_research_requests(attempt_id);''')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def begin(self,row):
        key=hashlib.sha256(json.dumps([self.manager,row['inn'],row['username'].lower().lstrip('@'),row['product']],ensure_ascii=False).encode()).hexdigest()
        attempt=uuid.uuid4().hex
        with self.db() as db:
            previous=db.execute('SELECT generated_json FROM legal_research_jobs WHERE job_key=?',(key,)).fetchone()
            cached=json.loads(previous[0]) if previous and previous[0] else None
            db.execute('INSERT INTO legal_research_jobs VALUES(?,?,?,?,?,NULL) ON CONFLICT(job_key) DO UPDATE SET row_json=excluded.row_json,state=excluded.state,updated_at=excluded.updated_at',
                (key,self.manager,json.dumps(row,ensure_ascii=False),'running',int(time.time())))
            db.execute('INSERT INTO legal_research_attempts(id,job_key,created_at,state) VALUES(?,?,?,?)',(attempt,key,int(time.time()),'running'))
        return key,attempt,cached
    def request(self,attempt,event):
        import config
        raw=json.dumps(event,ensure_ascii=False,default=str)
        if config.PARSER_API_KEY:raw=raw.replace(config.PARSER_API_KEY,'[REDACTED]')
        if len(raw)>2000000:raw=json.dumps(dict(endpoint=event.get('endpoint'),response_omitted='oversized'))
        with self.db() as db:db.execute('INSERT INTO legal_research_requests(attempt_id,created_at,event_json) VALUES(?,?,?)',(attempt,int(time.time()),raw))
    def finish(self,key,attempt,state,reason='',facts=None,model=None,generated=None):
        with self.db() as db:
            db.execute('UPDATE legal_research_attempts SET state=?,reason=?,facts_json=?,model_json=? WHERE id=?',
                (state,reason,json.dumps(facts,ensure_ascii=False),json.dumps(model,ensure_ascii=False),attempt))
            db.execute('UPDATE legal_research_jobs SET state=?,updated_at=?,generated_json=COALESCE(?,generated_json) WHERE job_key=?',
                (state,int(time.time()),json.dumps(generated,ensure_ascii=False) if generated else None,key))
    def retry_rows(self):
        with self.db() as db:
            rows=db.execute("SELECT job_key,row_json FROM legal_research_jobs WHERE manager=? AND state NOT IN ('ready','delivery_review') ORDER BY updated_at LIMIT 1000",(self.manager,)).fetchall()
            prepared=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='prepared_messages'").fetchone())
            result=[]
            for entry in rows:
                row=json.loads(entry['row_json'])
                saved=db.execute("SELECT status FROM prepared_messages WHERE manager_id=? AND inn=? AND lower(ltrim(username,'@'))=? AND product=? ORDER BY id DESC LIMIT 1",
                    (self.manager,row['inn'],row['username'].lower().lstrip('@'),row['product'])).fetchone() if prepared else None
                if saved:
                    state='ready' if saved[0] in ('prepared','sent') else 'delivery_review'
                    db.execute('UPDATE legal_research_jobs SET state=? WHERE job_key=?',(state,entry['job_key']))
                    continue  # Delivery uncertainty belongs to /queue, never to research retry.
                result.append(row)
        return result
    def ready(self,key):
        with self.db() as db:db.execute("UPDATE legal_research_jobs SET state='ready',updated_at=? WHERE job_key=? AND manager=?",(int(time.time()),key,self.manager))
    def service_notice(self,code):
        notice=hashlib.sha256(f'{self.manager}:{code}:{int(time.time())//3600}'.encode()).hexdigest()
        with self.db() as db:db.execute('INSERT OR IGNORE INTO legal_service_notices VALUES(?,?,?,?,NULL)',(notice,self.manager,code,int(time.time())))


async def flush_notices(client,authority,subject,public_key):
    """Signed acknowledgment through the existing technical bot; no client/court data."""
    import asyncio,base64
    import config
    from access_lease import canonical
    path=Path(config.DATABASE_PATH)
    if not path.exists():return
    # Do not initialize legal research storage for other businesses merely because sync runs.
    db=sqlite3.connect(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='legal_service_notices'").fetchone():return
        row=db.execute('SELECT notice_id,code FROM legal_service_notices WHERE manager=? AND sent_at IS NULL ORDER BY created_at LIMIT 1',(config.MANAGER_ID,)).fetchone()
    finally:db.close()
    if not row:return
    notice,code=row
    request=dict(subject=subject,notice_id=notice,code=code)
    wire=base64.urlsafe_b64encode(json.dumps(request,separators=(',',':')).encode()).decode()
    async with client.conversation(authority,timeout=12,exclusive=True) as conversation:
        sent=await conversation.send_message('/research_status '+wire,silent=True)
        async with asyncio.timeout(12):
            while True:
                response=await conversation.get_response(sent)
                text=response.raw_text or ''
                if not text.startswith('DVK_RESEARCH_ACK '):continue
                try:
                    envelope=json.loads(text[len('DVK_RESEARCH_ACK '):]);payload=envelope['payload']
                    public_key.verify(base64.b64decode(envelope['signature'],validate=True),canonical(payload))
                    if payload!=dict(protocol='DVK_RESEARCH_ACK_V1',subject=subject,notice_id=notice):continue
                    break
                except Exception:continue
    db=sqlite3.connect(path)
    try:
        with db:db.execute('UPDATE legal_service_notices SET sent_at=? WHERE notice_id=? AND manager=?',(int(time.time()),notice,config.MANAGER_ID))
    finally:db.close()


def run(row,sender,settings,prompt,client_factory):
    import config
    import arbitration_api as api
    manager=config.MANAGER_ID.strip()
    if not manager:raise ResearchProblem('Не настроен номер менеджера.','research_failed','configuration')
    row={**row,'inn':api._normalize_inn(row['inn']),'username':row['username'].lower().lstrip('@')}
    try:journal=Journal(manager)
    except (sqlite3.Error,OSError):raise ResearchProblem('Не удалось открыть журнал исследования. Подготовка остановлена.','persistence_failed','storage') from None
    key_material=json.dumps([manager,row['inn'],row['username'],row['product']])
    with _locks_guard:lock=_locks.setdefault(key_material,threading.Lock())
    with lock:
        try:key,attempt,cached=journal.begin(row)
        except (sqlite3.Error,OSError):raise ResearchProblem('Не удалось сохранить начало исследования. Подготовка остановлена.','persistence_failed','storage') from None
        facts=None;model=[]
        try:
            if len(row['inn'])!=(12 if row['product']=='Право_ИП' else 10):
                raise ResearchProblem('Длина ИНН не соответствует выбранному продукту ООО/ИП.')
            with api.observe_requests(lambda event:journal.request(attempt,event)):
                result=api.find_latest_defendant_case(row['inn'])
            facts=verified_facts(result,row['inn'])
            fingerprint=hashlib.sha256(json.dumps([PIPELINE_REVISION,facts,sender,prompt,settings['model'],GREETINGS,OFFERS],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if cached and cached.get('fingerprint')==fingerprint:
                analysis=cached['analysis'];analysis['legal_job_key']=key
                journal.finish(key,attempt,'generated',facts=facts,generated=cached)
                return analysis
            facts_json=json.dumps(facts,ensure_ascii=False,indent=2)
            actual_prompt=(prompt+'\n\nПРОВЕРЕННЫЕ СУДЕБНЫЕ ФАКТЫ:\n'+facts_json+
                '\nЭти реквизиты уже проверены программой. Не ищи другое дело. Недоступность веб-поиска не отменяет эти данные. '
                'Сохрани дословно номер, названия суда и сторон, сумму в рублях, название и дату события. '
                'Не называй поступление заявления принятием иска. Не выдумывай должность адресата; допустимо нейтральное «Здравствуйте!». '
                'Имя отправителя: '+sender+'. Только имя, без фамилии. Сформируй короткое предложение юридической помощи без гарантий. '
                'Судебный абзац собирает программа; ты не можешь менять его или дописывать факты. '
                'Выбери только подачу: greeting из '+json.dumps(GREETINGS,ensure_ascii=False)+
                ' и offer_index из вариантов '+json.dumps(dict(enumerate(OFFERS)),ensure_ascii=False)+
                '. Верни только JSON с этими двумя полями; эта схема вывода заменяет формат из бизнес-промпта.')
            text=None;fallback=False
            if not config.OPENAI_API_KEY:raise ResearchProblem('Не задан ключ ИИ.','research_paused','configuration')
            writer_key=hashlib.sha256(config.OPENAI_API_KEY.encode()).hexdigest()
            with _locks_guard:writer_available=_writer_cooldowns.get(writer_key,0)<=time.monotonic()
            if not writer_available:model.append(dict(error_type='WriterCooldown',fallback='approved facts template'))
            for generation in range(2 if writer_available else 0):
                try:
                    client=client_factory(api_key=config.OPENAI_API_KEY,timeout=45,max_retries=0)
                    try:response=client.responses.create(model=settings['model'],tools=[],input=actual_prompt,
                        text={'format':{'type':'json_schema','name':'legal_opening','strict':True,'schema':STYLE_SCHEMA}})
                    finally:client.close()
                    try:
                        from service_reports_runtime import record_usage
                        record_usage(Path.cwd(),phase='legal_research',model=settings['model'],result=response,status='received',product=row['product'])
                    except Exception:pass
                    raw=response.output_text or ''
                    model.append(dict(raw=raw))
                    style=json.loads(raw)
                    if (not isinstance(style,dict) or set(style)!={'greeting','offer_index'}
                            or style['greeting'] not in GREETINGS or type(style['offer_index']) is not int
                            or style['offer_index'] not in (0,1,2)):raise ValueError('Invalid legal style')
                    proposal=fallback_message(facts,sender,style['greeting'],OFFERS[style['offer_index']])
                    errors=validate_message(proposal,facts,sender)
                    if not errors:
                        text=proposal
                        with _locks_guard:_writer_cooldowns.pop(writer_key,None)
                        break
                    model[-1]['validation_errors']=errors
                except Exception as error:
                    # Preserve the actual manual-review prose above, but not credential-bearing exceptions.
                    errors=['Ответ не прошёл проверку: '+type(error).__name__]
                    model.append(dict(error_type=type(error).__name__))
                    if not isinstance(error,ValueError):
                        with _locks_guard:_writer_cooldowns[writer_key]=time.monotonic()+60
                        break  # An unavailable writer must not delay every row twice.
                actual_prompt+='\n\nИсправь только эти нарушения: '+'; '.join(errors)+'. Не добавляй отсутствующие факты.'
            if text is None:
                # The approved neutral template is possible only after the same strict facts gate.
                text=fallback_message(facts,sender);fallback=True
                if validate_message(text,facts,sender):raise ResearchProblem('Проверенные факты не удалось безопасно разместить в сообщении.')
            analysis=dict(raw_result='ГОТОВОЕ СООБЩЕНИЕ\n'+text+'\nКОНЕЦ СООБЩЕНИЯ',final_message=text,
                product=row['product'],prompt_file=settings['prompt'],prompt_text=actual_prompt,
                model=settings['model'],product_version=settings['version'],legal_job_key=key,
                legal_fallback=fallback,legal_attempt_id=attempt)
            journal.finish(key,attempt,'generated',facts=facts,model=model,generated=dict(fingerprint=fingerprint,analysis=analysis))
            return analysis
        except ResearchProblem as problem:
            journal.finish(key,attempt,problem.status,str(problem),facts,model)
            if problem.code in ('quota','credentials'):journal.service_notice(problem.code)
            raise
        except (sqlite3.Error,OSError):
            raise ResearchProblem('Не удалось сохранить журнал исследования. Отправка не разрешена.','persistence_failed','storage') from None
        except Exception as error:
            problem=ResearchProblem('Не удалось завершить исследование ('+type(error).__name__+'). Подробности сохранены.','research_failed','unexpected')
            journal.finish(key,attempt,problem.status,str(problem),facts,model)
            raise problem from None
