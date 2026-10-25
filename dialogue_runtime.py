"""Explicit finance dialogue scope. Absent configuration means no dialogue work.

The existing first-contact database supplies proof of an actual outbound send.
Only configured or locally verified sent contacts are read, never personal-chat discovery.
"""
import asyncio
import base64
from datetime import datetime,timezone
import json
import logging
import os
from pathlib import Path
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dialogue_policy import DialoguePolicy
from dialogue_store import DialogueStore,StaleDialogue
from dialogue_engine import DialogueEngine
from dialogue_ai import DialogueAI
from dialogue_knowledge import KnowledgeUnavailable
from dialogue_knowledge_transport import RemoteKnowledge
from dialogue_manager_ui import register as register_ui,deliver_notices,controls
from dialogue_send_gate import DeliveryGate
from telethon import events
from telethon.errors import FloodWaitError,PeerFloodError
from access_lease import AccessDenied

log=logging.getLogger(__name__)


def original_text_matches(actual,saved):
    if actual==saved:return True
    from telethon.extensions.markdown import parse
    return actual==parse(saved)[0]


def read_settings(root):
    path=Path(root)/'data/dialogue-runtime.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>16000:raise ValueError('Unsafe dialogue settings')
    value=json.loads(path.read_text('utf-8'))
    if value.get('enabled') is False:return None
    pilot=(value.get('mode')=='pilot' and type(value.get('peer_id')) is int and value['peer_id']>0
           and value.get('product') in {'БГ','Кредит'})
    ids=value.get('prepared_ids')
    cohort=(value.get('mode')=='selected_queue' and isinstance(ids,list) and 0<len(ids)<=1000
            and all(type(i) is int and i>0 for i in ids) and len(set(ids))==len(ids)
            and type(value.get('activated_at')) is int and value['activated_at']>0)
    if (value.get('format')!=1 or value.get('enabled') is not True or not (pilot or cohort)
            or value.get('business_id')!='dvk_finance'
            or not isinstance(value.get('company_id'),str) or not value['company_id']):
        raise ValueError('Invalid single-contact dialogue pilot settings')
    return value


def should_reply_at_activation(settings,history):
    return (settings.get('mode')=='selected_queue' and any(
        m['direction']=='incoming' and m['sent_at']>=settings['activated_at'] for m in history))


def eligible_contacts(db,manager_id,prepared_ids):
    """Only this frozen queue after a confirmed send; never other historical contacts."""
    if not prepared_ids:return []
    placeholders=','.join('?' for _ in prepared_ids)
    rows=db.execute('''SELECT c.telegram_id,c.product FROM clients c JOIN messages m ON m.client_id=c.id
        JOIN prepared_messages p ON p.manager_id=c.manager_id AND p.inn=c.inn
        AND lower(ltrim(p.username,'@'))=lower(ltrim(c.username,'@')) AND p.product=c.product
        AND p.final_message=m.text
        WHERE c.manager_id=? AND c.telegram_id>0 AND m.status='sent' AND p.status='sent'
        AND m.sent_at>=p.created_at AND p.id IN ('''+placeholders+''')
        AND m.telegram_message_id IS NOT NULL GROUP BY c.telegram_id,c.product
        ORDER BY c.telegram_id,c.product''',(manager_id,*prepared_ids)).fetchall()
    peers={}
    for peer,product in rows:peers.setdefault(peer,set()).add(product)
    return [(peer,next(iter(products))) for peer,products in peers.items()
            if len(products)==1 and next(iter(products)) in {'БГ','Кредит'}]


class DialogueRuntime:
    def __init__(self,client,application,root,settings,policy,ai_client,*,gate=None):
        import config,access_runtime,bot
        self.client,self.application,self.root=client,application,Path(root)
        self.settings,self.policy,self.ai_client=settings,policy,ai_client
        self.account_id=access_runtime.account_id
        self.manager_id=config.MANAGER_ID
        self.store=DialogueStore(self.root/config.DATABASE_PATH)
        self.store.initialize(sync=True)
        self.gate=gate if gate is not None else DeliveryGate(interval=bot.SEND_INTERVAL_SECONDS)
        self.previous_gate=bot.delivery_gate
        self.lock=asyncio.Lock()
        self.thread_id=None;self.peer=None
        self.poll_task=None;self.changed=asyncio.Event()
        self.remote=RemoteKnowledge(client,authority=os.environ['ACCESS_BOT_USERNAME'].lstrip('@'),
            subject=f'{self.manager_id}:{self.account_id}',encryption_public_key=os.environ['SYNC_PUBLIC_KEY'],
            signing_public_key=Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'],validate=True)),
            transport_lock=access_runtime.transport_lock,require_access=access_runtime.require_access)
        ai=DialogueAI(ai_client,model=self.policy.model,timezone=self.policy.timezone.key,prompts=self.policy.prompts)
        self.engine=DialogueEngine(self.store,policy,analyst=ai,writer=ai,knowledge=self.remote,
                                   require_access=access_runtime.require_access)
        self.event_handler=None
        if gate is None:bot.delivery_gate=self.gate

    async def enroll(self):
        from database import get_connection
        import access_runtime
        access_runtime.require_access()
        with get_connection() as db:
            row=db.execute('''SELECT m.telegram_message_id,m.text FROM messages m JOIN clients c ON c.id=m.client_id
                WHERE c.manager_id=? AND c.telegram_id=? AND c.product=? AND m.status='sent'
                AND m.telegram_message_id IS NOT NULL ORDER BY m.id LIMIT 1''',
                (self.manager_id,self.settings['peer_id'],self.settings['product'])).fetchone()
        if row is None:
            raise ValueError('Для тестового контакта нет подтверждённого первого сообщения по этому продукту.')
        self.peer=await self.client.get_input_entity(self.settings['peer_id'])
        original=await self.client.get_messages(self.peer,ids=row['telegram_message_id'])
        if (original is None or not original.out or not original_text_matches(original.raw_text,row['text'])
                or original.chat_id!=self.settings['peer_id']):
            raise ValueError('Первое сообщение не совпало с историей Telegram. Автоответы не включены.')
        self.thread_id=self.store.enroll(company_id=self.settings['company_id'],business_id=self.policy.business_id,
            manager_id=self.manager_id,account_id=self.account_id,peer_id=self.settings['peer_id'],
            product=self.settings['product'],first_message_id=original.id,first_sent_at=int(original.date.timestamp()))
        fresh=not self.store.thread(self.thread_id)['history_complete']
        new_incoming=await self.refresh(full=True,initial=fresh)
        self.store.mark_history_complete(self.thread_id)
        if fresh:
            thread,history=self.store.snapshot(self.thread_id)
            if thread['state'] not in {'human','opt_out','closed'} and any(
                    m['direction']=='outgoing' and m['telegram_id']!=original.id
                    and m['sent_at']>=thread['first_sent_at'] for m in history):
                self.store.set_state(self.thread_id,'human',expected_revision=thread['revision'])
            # Catch a quick reply to a newly sent queue message even if it arrives
            # before the once-per-minute enrollment scan. Older history is adopted quietly.
            if should_reply_at_activation(self.settings,history):self.changed.set()
            else:await self.adopt_history()
        elif new_incoming:
            self.changed.set()  # Catch up replies received while the program was off.
        if self.store.unresolved_actions(self.thread_id):
            thread,_=self.store.snapshot(self.thread_id)
            self.store.handoff(self.thread_id,event_key='interrupted_delivery',event_type='delivery',
                summary='Есть отправка с неопределённым результатом. Проверьте чат вручную; автодиалог приостановлен.',
                pause=True,expected_revision=thread['revision'])

    async def adopt_history(self):
        """Honor existing refusals/agreements without answering old messages at activation."""
        thread,history=self.store.snapshot(self.thread_id)
        incoming=[m for m in history if m['direction']=='incoming']
        if not incoming or thread['state'] in {'human','opt_out','closed'}:return
        try:
            decision=await self.engine.analyst.analyze(thread=thread,history=history,now=int(time.time()))
            if decision['intent']=='opt_out':
                self.store.set_state(self.thread_id,'opt_out',expected_revision=thread['revision'])
                return  # No delayed apology to an old refusal.
            relevant=incoming[-1]['sent_at']>=thread['first_sent_at']
            if not relevant:return
            if decision['intent'] in {'need','call','documents'}:
                self.store.handoff(self.thread_id,event_key='initial_'+decision['intent'],event_type=decision['intent'],
                    summary='В истории диалога: '+decision['summary'],pause=False,expected_revision=thread['revision'])
            if decision['intent']=='ready':
                self.store.handoff(self.thread_id,event_key='initial_ready',event_type='ready',
                    summary='В истории клиента выявлена готовность работать. '+decision['summary'],
                    pause=True,expected_revision=thread['revision'])
            elif decision['intent']=='later' and type(decision.get('return_at')) is int and decision['return_at']>time.time():
                self.store.set_state(self.thread_id,'later',expected_revision=thread['revision'],next_contact_at=decision['return_at'])
            else:
                self.store.set_state(self.thread_id,'engaged',expected_revision=thread['revision'])
        except KnowledgeUnavailable:
            self.store.handoff(self.thread_id,event_key='initial_unknown',event_type='knowledge_missing',
                summary='Не удалось надёжно разобрать существующую историю. Проверьте диалог; автоответы приостановлены.',
                pause=True,expected_revision=thread['revision'])

    def record(self,message,*,full=False):
        if getattr(message,'action',None):return False
        automatic=self.store.outgoing_text(self.thread_id,message.id) if message.out else None
        source=('history' if full else ('automation' if automatic is not None and automatic==message.raw_text else 'manager')) if message.out else 'client'
        text=message.raw_text or ''
        if message.media:
            # Never invent the contents of a photo, voice message or document.
            text+='\n[Вложение: содержимое автоматически не прочитано]'
        changed=self.store.record_message(self.thread_id,telegram_id=message.id,direction='outgoing' if message.out else 'incoming',
                                          text=text,sent_at=int(message.date.timestamp()),source=source)
        if changed and message.media and not message.out:
            thread=self.store.thread(self.thread_id)
            if int(message.date.timestamp())<thread['first_sent_at']:return not message.out
            self.store.handoff(self.thread_id,event_key='attachment:'+str(message.id),event_type='attachment',
                summary='Клиент прислал вложение. Нужно проверить его содержимое; автоответы приостановлены.',
                pause=True,expected_revision=thread['revision'])
        return changed and not message.out

    async def refresh(self,*,full=False,initial=False):
        minimum=0 if full else self.store.last_message_id(self.thread_id)
        incoming=False
        async for message in self.client.iter_messages(self.peer,reverse=True,min_id=minimum):
            incoming=self.record(message,full=initial) or incoming
        return incoming

    async def send(self,action):
        import bot,access_runtime
        async def operation():
            # Fetch messages arriving during the shared send interval before claiming.
            if await self.refresh():
                self.changed.set();return
            current=self.store.action_for_key(self.thread_id,action['action_key'])
            thread,_=self.store.snapshot(self.thread_id)
            allowed=(thread['state']=='opt_out' if action['kind']=='opt_out_ack' else thread['state'] in {'waiting','engaged','later'})
            if not current or current['status']!='pending' or not allowed:return
            self.store.claim_action(action['id'])
            try:
                sent=await self.client.send_message(self.peer,action['text'],parse_mode=None)
            except Exception as exc:
                self.store.finish_action(action['id'],status='uncertain')
                thread,_=self.store.snapshot(self.thread_id)
                self.store.handoff(self.thread_id,event_key='delivery:'+action['id'],event_type='delivery',
                    summary='Результат отправки не подтверждён. Проверьте чат вручную; автоматического повтора нет.',
                    pause=True,expected_revision=thread['revision'])
                if isinstance(exc,PeerFloodError):raise bot.pause_for_spam() from exc
                if isinstance(exc,FloodWaitError):raise bot.pause_for_flood(exc) from exc
                return
            self.store.finish_action(action['id'],status='sent',telegram_id=sent.id)
            self.record(sent)
        def guard():
            access_runtime.require_access();bot.require_send_cooldown_finished()
        await self.gate.run(operation,guard)

    async def run(self):
        import bot,access_runtime
        try:
            await self.enroll()
            register_ui(self.application,self.store,account_id=self.account_id,manager_id=self.manager_id)
            thread=self.store.thread(self.thread_id)
            await self.application.bot.send_message(chat_id=self.account_id,
                text=f"Тестовый диалог подключён: {self.store.client_label(self.thread_id)}. Управление — /dialogues.",
                reply_markup=controls(self.thread_id,thread['revision'],thread['state']))
            async def handler(event):
                if not event.is_private or event.chat_id!=self.settings['peer_id']:return
                async with self.lock:
                    if self.record(event.message):self.changed.set()
            self.event_handler=handler
            self.client.add_event_handler(handler,events.NewMessage(chats=[self.settings['peer_id']]))
            self.client.add_event_handler(handler,events.MessageEdited(chats=[self.settings['peer_id']]))
            # No reply to old history on startup. Only newly received messages trigger replies.
            while True:
                try:
                    async with self.lock:
                        access_runtime.require_access();bot.require_send_cooldown_finished()
                        if await self.refresh():self.changed.set()
                    if self.changed.is_set():
                        await asyncio.sleep(3)  # Group a customer's consecutive short messages.
                        async with self.lock:
                            self.changed.clear();await self.refresh()
                            await self.engine.on_incoming(self.thread_id)
                    async with self.lock:
                        await self.engine.on_silence(self.thread_id)
                        await self.engine.on_return(self.thread_id)
                        for action in self.store.pending_actions(self.account_id):
                            if action['thread_id']==self.thread_id:
                                await self.send(action)
                except (AccessDenied,bot.TelegramSendPause,StaleDialogue):
                    pass
                except PeerFloodError as exc:
                    bot.pause_for_spam()
                except FloodWaitError as exc:
                    bot.pause_for_flood(exc)
                except Exception as exc:
                    log.warning('Dialogue paused (%s)',type(exc).__name__)
                    thread,_=self.store.snapshot(self.thread_id)
                    self.store.handoff(self.thread_id,event_key='runtime:'+str(thread['revision']),event_type='runtime',
                        summary='Не удалось продолжить диалог автоматически. Нужна проверка менеджера.',
                        pause=True,expected_revision=thread['revision'])
                try:
                    await deliver_notices(self.application.bot,self.store,account_id=self.account_id)
                except Exception as exc:
                    log.warning('Dialogue notification deferred (%s)',type(exc).__name__)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Dialogue pilot not started (%s)',type(exc).__name__)
            await self.application.bot.send_message(chat_id=self.account_id,
                text='Тестовый автодиалог не включён. Первая часть работает; нужна проверка настройки или истории тестового контакта.')
        finally:
            if self.event_handler:self.client.remove_event_handler(self.event_handler)
            bot.delivery_gate=self.previous_gate
            await self.ai_client.close()


class SelectedQueueRuntime:
    """One serialized scheduler and delivery gate for verified local finance contacts.

    Enrollment is gradual, never a search of the account's other private chats.
    Each thread keeps its own full history, revision, opt-out and manager controls.
    """
    def __init__(self,client,application,root,settings,policy,ai_client):
        import bot,config,access_runtime
        self.client,self.application,self.root=client,application,Path(root)
        self.settings,self.policy,self.ai_client=settings,policy,ai_client
        self.account_id,self.manager_id=access_runtime.account_id,config.MANAGER_ID
        self.store=DialogueStore(self.root/config.DATABASE_PATH);self.store.initialize(sync=True)
        self.gate=DeliveryGate(interval=bot.SEND_INTERVAL_SECONDS)
        self.previous_gate=bot.delivery_gate;bot.delivery_gate=self.gate
        self.workers={};self.attempted=set();self.scanned_at={};self.cursor=0
        self.handler=None;self.lock=asyncio.Lock()

    async def add_contact(self,peer,product):
        worker=DialogueRuntime(self.client,self.application,self.root,
            {**self.settings,'peer_id':peer,'product':product},self.policy,self.ai_client,gate=self.gate)
        await worker.enroll()
        self.workers[peer]=worker;self.scanned_at[peer]=time.monotonic()
        return worker

    async def service(self,worker):
        import bot,access_runtime
        async with worker.lock:
            access_runtime.require_access();bot.require_send_cooldown_finished()
            peer=worker.settings['peer_id']
            # New/edit events wake this thread immediately. Reconcile gaps slowly,
            # instead of issuing hundreds of history requests every five seconds.
            if time.monotonic()-self.scanned_at[peer]>=300:
                if await worker.refresh():worker.changed.set()
                self.scanned_at[peer]=time.monotonic()
            if worker.changed.is_set():
                await asyncio.sleep(3)
                worker.changed.clear();await worker.refresh()
                await worker.engine.on_incoming(worker.thread_id)
            await worker.engine.on_silence(worker.thread_id)
            await worker.engine.on_return(worker.thread_id)
            for action in worker.store.pending_actions(self.account_id):
                if action['thread_id']==worker.thread_id:await worker.send(action)

    async def run(self):
        import bot,access_runtime
        from database import get_connection
        last_discovery=0;pending=[];enrollment_blocked=False;reported=False
        try:
            register_ui(self.application,self.store,account_id=self.account_id,manager_id=self.manager_id)
            async def handler(event):
                worker=self.workers.get(event.chat_id) if event.is_private else None
                if worker is None:return
                async with worker.lock:
                    if worker.record(event.message):worker.changed.set()
            self.handler=handler
            # No username resolution for unknown senders, no reads of their history.
            self.client.add_event_handler(handler,events.NewMessage())
            self.client.add_event_handler(handler,events.MessageEdited())
            await self.application.bot.send_message(chat_id=self.account_id,
                text=f"Сопровождение включено только для выбранной очереди ({len(self.settings['prepared_ids'])} записей). Диалог подключается после подтверждённой отправки. Старая база не подключается. Управление — /dialogues.")
            while True:
                worker=None
                try:
                    access_runtime.require_access();bot.require_send_cooldown_finished()
                    if not enrollment_blocked and time.monotonic()-last_discovery>=60:
                        with get_connection() as db:candidates=eligible_contacts(db,self.manager_id,self.settings['prepared_ids'])
                        pending=[item for item in candidates if item[0] not in self.attempted]
                        last_discovery=time.monotonic()
                    # Replies take priority over background enrollment and reminders.
                    worker=next((w for w in self.workers.values() if w.changed.is_set()),None)
                    if worker is None and pending and not enrollment_blocked:
                        peer,product=pending.pop(0);self.attempted.add(peer)
                        try:
                            await self.add_contact(peer,product)
                        except (PeerFloodError,FloodWaitError):
                            enrollment_blocked=True;raise
                        except AccessDenied:
                            self.attempted.discard(peer);raise
                        except Exception as exc:
                            log.warning('Contact history enrollment deferred (%s)',type(exc).__name__)
                            await self.application.bot.send_message(chat_id=self.account_id,
                                text=f'Автообщение не включено для Telegram ID {peer}: история не подтверждена. Нужна проверка. Тип: {type(exc).__name__}.')
                    if worker is None and self.workers:
                        workers=list(self.workers.values());worker=workers[self.cursor%len(workers)];self.cursor+=1
                    if worker is not None:await self.service(worker)
                    if self.attempted and not pending and not reported:
                        reported=True
                        await self.application.bot.send_message(chat_id=self.account_id,
                            text=f'Сейчас подключено диалогов: {len(self.workers)}. Не подключено после проверки: {len(self.attempted)-len(self.workers)}. Остальные участники выбранной очереди подключатся после подтверждённой отправки. /dialogues — управление.')
                except (AccessDenied,bot.TelegramSendPause,StaleDialogue):pass
                except PeerFloodError:
                    bot.pause_for_spam()
                except FloodWaitError as exc:
                    bot.pause_for_flood(exc)
                except Exception as exc:
                    log.warning('Known-contact dialogue deferred (%s)',type(exc).__name__)
                    if worker is not None:
                        thread=worker.store.thread(worker.thread_id)
                        worker.store.handoff(worker.thread_id,event_key='runtime:'+str(thread['revision']),event_type='runtime',
                            summary='Не удалось продолжить диалог автоматически. Нужна проверка менеджера.',
                            pause=True,expected_revision=thread['revision'])
                try:await deliver_notices(self.application.bot,self.store,account_id=self.account_id)
                except Exception as exc:log.warning('Dialogue notification deferred (%s)',type(exc).__name__)
                await asyncio.sleep(2)
        finally:
            if self.handler:self.client.remove_event_handler(self.handler)
            bot.delivery_gate=self.previous_gate
            await self.ai_client.close()


async def start(client,application,root=Path('.')):
    settings=read_settings(root)
    if settings is None:return None
    policy=DialoguePolicy.load(Path(root)/'dialogue_profiles/dvk_finance.json')
    policy.enabled=True  # Explicit private pilot configuration, never the distributed default.
    import config
    from openai import AsyncOpenAI
    ai_client=AsyncOpenAI(api_key=config.OPENAI_API_KEY,timeout=60,max_retries=0)
    try:
        runtime_class=SelectedQueueRuntime if settings['mode']=='selected_queue' else DialogueRuntime
        runtime=runtime_class(client,application,root,settings,policy,ai_client)
    except Exception:
        await ai_client.close()
        raise
    return asyncio.create_task(runtime.run(),name='finance-dialogue-runtime')
