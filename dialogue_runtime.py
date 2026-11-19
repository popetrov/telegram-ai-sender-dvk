from finance_scope import require_access as require_finance_access
from finance_scope import account_allowed, thread_allowed, current_account, current_manager
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
import re
from pathlib import Path
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dialogue_policy import DialoguePolicy
from dialogue_store import DialogueStore,StaleDialogue
from dialogue_engine import DialogueEngine
from dialogue_ai import DialogueAI
from dialogue_knowledge import KnowledgeUnavailable,KnowledgeTransportUnavailable
from dialogue_knowledge_transport import RemoteKnowledge
from dialogue_manager_ui import register as register_ui,deliver_notices,controls,dialogue_card
from dialogue_send_gate import DeliveryGate
from telethon import events
from telethon.errors import FloodWaitError,PeerFloodError
from access_lease import AccessDenied
from dialogue_transport_retry import TRANSIENT_ERRORS, record_retry
from dialogue_seller import OWNER_ACCOUNT_ID,SellerControls,can_send

log=logging.getLogger(__name__)


class DeletedHistory(ValueError):
    """A deleted chat/message is a permanent no-send condition, never an invitation to retry."""


class AwaitingIncoming(ValueError):
    """Explicit inbound pilot is waiting; no synthetic first outbound message."""


def original_text_matches(actual,saved):
    if actual==saved:return True
    from telethon.extensions.markdown import parse
    return actual==parse(saved)[0]


def read_settings(root):
    path=Path(root)/'data/dialogue-runtime.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>256000:raise ValueError('Unsafe dialogue settings')
    value=json.loads(path.read_text('utf-8'))
    if value.get('enabled') is False:return None
    pilot=(value.get('mode')=='pilot' and type(value.get('peer_id')) is int and value['peer_id']>0
           and value.get('product') in {'БГ','Кредит'})
    ids=value.get('prepared_ids')
    cohort=(value.get('mode')=='selected_queue' and isinstance(ids,list) and 0<len(ids)<=1000
            and all(type(i) is int and i>0 for i in ids) and len(set(ids))==len(ids)
            and type(value.get('activated_at')) is int and value['activated_at']>0)
    contacts=value.get('contacts')
    future=(value.get('mode')=='owner_outbound' and value.get('seller_stage1') is True
            and type(value.get('after_message_id')) is int and value['after_message_id']>=0
            and account_allowed(value.get('owner_account_id'))
            and type(value.get('activated_at')) is int and value['activated_at']>0)
    base=(value.get('mode')=='reviewed_base' and isinstance(contacts,list) and 0<len(contacts)<=10000
          and all(isinstance(c,dict) and set(c)=={'peer_id','product'} and type(c['peer_id']) is int
                  and c['peer_id']>0 and c['product'] in {'БГ','Кредит'} for c in contacts)
          and len({c['peer_id'] for c in contacts})==len(contacts)
          and isinstance(value.get('review_id'),str) and len(value['review_id'])==32
          and all(c in '0123456789abcdef' for c in value['review_id']))
    if (value.get('format')!=1 or value.get('enabled') is not True or not (pilot or cohort or base or future)
            or value.get('business_id')!='dvk_finance'
            or not isinstance(value.get('company_id'),str) or not value['company_id']):
        raise ValueError('Invalid single-contact dialogue pilot settings')
    if 'seller_stage1' in value:
        peers=value.get('allowed_peer_ids')
        if (type(value['seller_stage1']) is not bool or (value['seller_stage1'] and
                (not future and (base or not isinstance(peers,list) or not 0<len(peers)<=20
                 or any(type(p) is not int or p<=0 for p in peers) or len(set(peers))!=len(peers)
                 or (pilot and value['peer_id'] not in peers))))):
            raise ValueError('Пилот продавца требует отдельного списка разрешённых чатов (до 20).')
    if value.get('watch_all_history') and (not future or type(value.get('history_watch_started_at')) is not int or value['history_watch_started_at']<=0 or value.get('reply_only') is not True):
        raise ValueError('Отслеживание базы требует времени включения и режима только ответов.')
    if value.get('attachments_enabled') and (not future or type(value.get('attachments_started_at')) is not int or value['attachments_started_at']<=0 or value.get('attachment_audio_model')!='gpt-4o-transcribe'):
        raise ValueError('Чтение вложений требует явной локальной настройки владельца.')
    test=value.get('owner_test_contact')
    if test is not None and (not future or not isinstance(test,dict) or set(test)!={'peer_id','product','activated_at'} or test['peer_id']!=6722235434 or test['product'] not in {'БГ','Кредит'} or type(test['activated_at']) is not int or test['activated_at']<=0):
        raise ValueError('Некорректный локальный тестовый контакт владельца.')
    if value.get('stage3_live_enabled') and not value.get('stage3_calendar_enabled'):
        raise ValueError('Планировщик не может работать без календаря.')
    if 'stage3_test_peer_id' in value and (not future or type(value['stage3_test_peer_id']) is not int or value['stage3_test_peer_id']<=0):
        raise ValueError('Некорректный тестовый контакт календаря.')
    if 'stage3_calendar_enabled' in value and (not future or type(value['stage3_calendar_enabled']) is not bool or type(value.get('stage3_live_enabled',False)) is not bool):
        raise ValueError('Календарь доступен только в локальном режиме владельца.')
    if 'anchor_kind' in value:
        if (value['anchor_kind']!='incoming' or value.get('seller_stage1') is not True or not pilot
                or type(value.get('activated_at')) is not int or value['activated_at']<=0
                or value['allowed_peer_ids']!=[value['peer_id']]):
            raise ValueError('Входящий пилот требует одного явно выбранного контакта и времени подключения.')
    return value


def historical_contacts(db,manager_id):
    rows=db.execute('''SELECT c.telegram_id,c.product FROM clients c JOIN messages m ON m.client_id=c.id
        WHERE c.manager_id=? AND c.telegram_id>0 AND m.status='sent' AND m.telegram_message_id IS NOT NULL
        GROUP BY c.telegram_id,c.product ORDER BY c.telegram_id,c.product''',(manager_id,)).fetchall()
    peers={}
    for peer,product in rows:peers.setdefault(peer,set()).add(product)
    return [dict(peer_id=peer,product=next(iter(products))) for peer,products in peers.items()
            if len(products)==1 and next(iter(products)) in {'БГ','Кредит'}]


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


def future_contacts(db,manager_id,after_message_id,watch_all_history=False):
    """Explicit owner scope: historical monitoring only when separately enabled."""
    if watch_all_history:
        rows=db.execute("""SELECT c.telegram_id,c.product FROM clients c JOIN messages m ON m.client_id=c.id
            WHERE c.manager_id=? AND c.telegram_id>0 AND m.status='sent'
            AND m.telegram_message_id IS NOT NULL AND c.product IN ('БГ','Кредит')
            ORDER BY m.id DESC""",(manager_id,)).fetchall()
        peers={}
        for peer,product in rows:peers.setdefault(peer,product)
        for t in db.execute("SELECT peer_id,product FROM dialogue_threads WHERE manager_id=? AND account_id=? AND business_id='dvk_finance'",(manager_id,current_account())):
            if t[0] in peers and t[1] in {'БГ','Кредит'}:peers[t[0]]=t[1]
        return sorted(peers.items())
    rows=db.execute('''SELECT c.telegram_id,c.product FROM clients c JOIN messages m ON m.client_id=c.id
        WHERE c.manager_id=? AND c.telegram_id>0 AND m.id>? AND m.status='sent'
        AND m.telegram_message_id IS NOT NULL GROUP BY c.telegram_id,c.product''',
        (manager_id,after_message_id)).fetchall()
    peers={}
    for peer,product in rows:peers.setdefault(peer,set()).add(product)
    return [(peer,next(iter(products))) for peer,products in peers.items()
            if len(products)==1 and next(iter(products)) in {'БГ','Кредит'}]


def seller_contacts(db,manager_id,settings):
    contacts=future_contacts(db,manager_id,settings['after_message_id'],settings.get('watch_all_history',False))
    test=settings.get('owner_test_contact')
    if test and test['peer_id'] not in {p for p,_ in contacts}:contacts.append((test['peer_id'],test['product']))
    return contacts


class DialogueRuntime:
    def __init__(self,client,application,root,settings,policy,ai_client,*,gate=None):
        import config,access_runtime,bot
        self.client,self.application,self.root=client,application,Path(root)
        self.settings,self.policy,self.ai_client=settings,policy,ai_client
        self.account_id=access_runtime.account_id
        self.manager_id=config.MANAGER_ID
        if settings.get('seller_stage1') and (not account_allowed(self.account_id)
                or (settings.get('mode')!='owner_outbound' and settings['peer_id'] not in settings['allowed_peer_ids'])):
            raise PermissionError('Пилот продавца доступен только владельцу и разрешённым чатам.')
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
            transport_lock=access_runtime.transport_lock,require_access=require_finance_access)
        from dialogue_records import usage as record_usage
        round_start=settings.get('test_context_start_id',0)
        if round_start and (not settings.get('synthetic_test') or not account_allowed(self.account_id)):
            raise PermissionError('New-round context is only allowed in the owner synthetic test')
        from dialogue_methodology import load as load_methodology
        methodology=load_methodology(self.root,self.policy.primary_methodology,business_id=self.policy.business_id) if settings.get('seller_stage1') else []
        ai=DialogueAI(ai_client,model=self.policy.model,timezone=self.policy.timezone.key,prompts=self.policy.prompts,
                      products=self.policy.products,ground_numbers=self.policy.ground_numbers,reply_style=self.policy.reply_style,
                      seller_memory=self.policy.seller_memory,seller_applications=self.policy.seller_applications,case_learning=self.policy.case_learning,test_context_start_id=round_start,primary_methodology=methodology,methodology_focus_rules=self.policy.methodology_focus_rules,complete_agreements=self.policy.complete_agreements,
                      usage_recorder=(lambda **values:record_usage(self.store,self.thread_id,**values))
                          if settings.get('seller_stage1') else None)
        self.engine=DialogueEngine(self.store,policy,analyst=ai,writer=ai,knowledge=self.remote,
                                   require_access=require_finance_access)
        self.engine.calendar_settings={**settings,'stage3_live_enabled':bool(settings.get('stage3_live_enabled') and (not settings.get('stage3_test_peer_id') or settings.get('peer_id')==settings['stage3_test_peer_id']))}
        self.event_handler=None;self.delete_handler=None
        self.inflight=False;self.outgoing_buffer=[]
        self.latest_incoming_at=None
        if gate is None:bot.delivery_gate=self.gate

    async def enroll(self):
        if self.settings.get('anchor_kind')=='incoming':
            return await self.enroll_incoming()
        from database import get_connection
        import access_runtime
        require_finance_access()
        with get_connection() as db:
            row=db.execute('''SELECT m.telegram_message_id,m.text FROM messages m JOIN clients c ON c.id=m.client_id
                WHERE c.manager_id=? AND c.telegram_id=? AND c.product=? AND m.status='sent'
                AND m.telegram_message_id IS NOT NULL ORDER BY m.id LIMIT 1''',
                (self.manager_id,self.settings['peer_id'],self.settings['product'])).fetchone()
            if self.settings.get('mode')=='owner_outbound':
                row=db.execute('''SELECT m.telegram_message_id,m.text FROM messages m JOIN clients c ON c.id=m.client_id
                    WHERE c.manager_id=? AND c.telegram_id=? AND c.product=? AND m.status='sent'
                    AND m.telegram_message_id IS NOT NULL AND m.id>? ORDER BY m.id LIMIT 1''',
                    (self.manager_id,self.settings['peer_id'],self.settings['product'],0 if self.settings.get('watch_all_history') else self.settings['after_message_id'])).fetchone()
        if self.settings.get('watch_all_history'):
            prior=next((t for t in self.store.threads(self.account_id) if t['peer_id']==self.settings['peer_id']),None)
            if prior:
                with get_connection() as db:
                    row=db.execute("""SELECT m.telegram_message_id,m.text FROM messages m JOIN clients c ON c.id=m.client_id
                        WHERE c.manager_id=? AND c.telegram_id=? AND c.product=? AND m.status='sent'
                        AND m.telegram_message_id=? LIMIT 1""",(self.manager_id,self.settings['peer_id'],self.settings['product'],prior['first_message_id'])).fetchone()
        if row is None:
            raise ValueError('Для тестового контакта нет подтверждённого первого сообщения по этому продукту.')
        self.peer=await self.client.get_input_entity(self.settings['peer_id'])
        original=await self.client.get_messages(self.peer,ids=row['telegram_message_id'])
        if original is None or not getattr(original,'id',None) or type(original).__name__=='MessageEmpty':
            for thread in self.store.threads(self.account_id):
                if thread['peer_id']==self.settings['peer_id']:
                    self.thread_id=thread['id'];self.close_deleted()
            raise DeletedHistory('Первое сообщение удалено. Писать этому клиенту запрещено.')
        if (not original.out or not original_text_matches(original.raw_text,row['text'])
                or original.chat_id!=self.settings['peer_id']):
            raise ValueError('Первое сообщение не совпало с историей Telegram. Автоответы не включены.')
        self.thread_id=self.store.enroll(company_id=self.settings['company_id'],business_id=self.policy.business_id,
            manager_id=self.manager_id,account_id=self.account_id,peer_id=self.settings['peer_id'],
            product=self.settings['product'],first_message_id=original.id,first_sent_at=int(original.date.timestamp()))
        fresh=not self.store.thread(self.thread_id)['history_complete']
        new_incoming=await self.refresh(full=True,initial=fresh)
        if original.id not in {m['telegram_id'] for m in self.store.snapshot(self.thread_id)[1]}:
            self.close_deleted();raise DeletedHistory('Первое сообщение отсутствует в прочитанной истории.')
        self.store.mark_history_complete(self.thread_id)
        if self.settings.get('seller_stage1'):
            thread=self.store.thread(self.thread_id)
            if self.store.unresolved_actions(self.thread_id):
                self.store.handoff(self.thread_id,event_key='interrupted_delivery',event_type='delivery',
                    summary='Есть отправка с неопределённым результатом. Проверьте чат вручную; автодиалог приостановлен.',
                    pause=True,expected_revision=thread['revision'])
                return
            if self.settings.get('watch_all_history') and not thread['seller_enabled']:
                await self.activate_history_watch()
                return
            if not thread['seller_enabled']:
                SellerControls(self.store).enable(self.thread_id,actor=self.account_id,
                    manager=self.manager_id,revision=thread['revision'])
                if self.settings.get('mode')=='owner_outbound':
                    await self.activate_new_campaign(original.id)
            elif not fresh:
                _,saved_history=self.store.snapshot(self.thread_id)
                latest=next((m for m in reversed(saved_history) if m['direction']=='incoming'),None)
                if new_incoming or (latest and latest['telegram_id']>max(thread['processed_incoming_id'],thread['incoming_floor_id'])):
                    self.changed.set()  # Also recover an incoming persisted before an interrupted analysis.
            # Existing history is context, never an instruction to send on activation.
            return
        if self.settings.get('mode')=='reviewed_base':
            await self.review_existing()
            if new_incoming and not fresh:self.changed.set()
        elif fresh:
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

    async def activate_history_watch(self):
        """Arm future replies only; never reopen a stop or replay pre-activation turns."""
        t,history=self.store.snapshot(self.thread_id)
        if t['state'] in {'closed','opt_out'}:return
        if self.store.unresolved_actions(self.thread_id):return
        cutoff=self.settings['history_watch_started_at']
        old_disabled='Старый режим отключён при установке продавца для новых рассылок.'
        if t['control_reason'] and t['control_reason']!=old_disabled:return
        # Preserve explicit no-contact wording found in the historic conversation.
        norm=lambda value:re.sub(r'[\W_]+',' ',value.casefold()).strip()
        if any(' '+norm(p)+' ' in ' '+norm(m['text'])+' '
               for m in history if m['direction']=='incoming' for p in self.policy.contact_stop_phrases):
            self.store.set_state(self.thread_id,'opt_out',expected_revision=t['revision']);return
        SellerControls(self.store).enable(self.thread_id,actor=self.account_id,manager=self.manager_id,revision=t['revision'])
        t=self.store.thread(self.thread_id)
        recent=[m for m in history if m['sent_at']>=cutoff and m['telegram_id']!=t['first_message_id']]
        if any(m['direction']=='outgoing' or ('[Вложение:' in m['text'] and not getattr(self,'settings',{}).get('attachments_enabled')) for m in recent):
            self.store.handoff(self.thread_id,event_key='history_watch_manual',event_type='history',
                summary='В новом сообщении есть вложение или после включения уже ответил менеджер. Проверьте диалог.',
                pause=True,expected_revision=t['revision']);return
        floor=max([t['first_message_id']]+[m['telegram_id'] for m in history if m['direction']=='incoming' and m['sent_at']<cutoff])
        with self.store.connection() as db:
            db.execute("UPDATE dialogue_threads SET incoming_floor_id=MAX(incoming_floor_id,?),processed_incoming_id=MAX(processed_incoming_id,?) WHERE id=?",(floor,floor,self.thread_id))
        SellerControls(self.store).set_mode(self.thread_id,'active_negotiation',actor=self.account_id,manager=self.manager_id,revision=t['revision'])
        with self.store.connection() as db:
            db.execute('UPDATE dialogue_threads SET control_reason=? WHERE id=?',
                ('Автоответы подключены после подтверждённой первичной отправки. Отслеживаются только новые ответы после включения всей финансовой базы.',self.thread_id))
        self.changed.set()

    async def activate_new_campaign(self,original_id):
        """First enrollment only. Preserve manual replies, closures, opt-outs and existing controls."""
        thread,history=self.store.snapshot(self.thread_id)
        later=[m for m in history if m['telegram_id']>original_id]
        if any(m['direction']=='outgoing' or ('[Вложение:' in m['text'] and not getattr(self,'settings',{}).get('attachments_enabled')) for m in later):
            self.store.handoff(self.thread_id,event_key='new_campaign_manual_review',event_type='history',
                summary='В новом диалоге уже есть ручной ответ или вложение. ИИ не включён; проверьте переписку и при необходимости верните ИИ.',
                pause=True,expected_revision=thread['revision'])
            return  # An already advanced conversation needs the manager's explicit return.
        with self.store.connection() as db:
            db.execute('UPDATE dialogue_threads SET incoming_floor_id=? WHERE id=?',(original_id,self.thread_id))
        SellerControls(self.store).set_mode(self.thread_id,'active_negotiation',actor=self.account_id,
            manager=self.manager_id,revision=thread['revision'])
        with self.store.connection() as db:
            db.execute("UPDATE dialogue_threads SET control_reason=? WHERE id=?",
                ('Автоответы подключены после подтверждённой первичной отправки. Ожидаю ответ клиента.',self.thread_id))
        self.changed.set()

    async def enroll_incoming(self):
        """Use a real incoming Telegram message, without a fabricated campaign row."""
        import access_runtime
        require_finance_access()
        if (not account_allowed(self.account_id) or not self.settings.get('seller_stage1')
                or self.settings.get('allowed_peer_ids')!=[self.settings['peer_id']]):
            raise PermissionError('Входящий пилот доступен только владельцу и одному выбранному чату.')
        self.peer=await self.client.get_input_entity(self.settings['peer_id'])
        previous=next((t for t in self.store.threads(self.account_id) if t['peer_id']==self.settings['peer_id']),None)
        if previous:
            if (previous['anchor_kind']!='incoming' or previous['manager_id']!=self.manager_id
                    or previous['product']!=self.settings['product'] or previous['company_id']!=self.settings['company_id']):
                raise ValueError('Существующий диалог имеет другую привязку. Она не заменена.')
            self.thread_id=previous['id']
            if previous['state'] in {'closed','opt_out'}:
                raise DeletedHistory('Существующий запрет сохранён; входящий пилот не возобновлён.')
            original=await self.client.get_messages(self.peer,ids=previous['first_message_id'])
        else:
            original=None
            # Catch a message received before handlers were installed, but never
            # adopt old history as a new request just because the sender is known.
            async for message in self.client.iter_messages(self.peer,limit=20):
                if int(message.date.timestamp())<self.settings['activated_at']:break
                if (not message.out and not getattr(message,'action',None)
                        and message.chat_id==self.settings['peer_id']):
                    original=message;break
            if original is None:raise AwaitingIncoming('Ожидаю новое входящее сообщение выбранного контакта.')
        if (original is None or not getattr(original,'id',None) or type(original).__name__=='MessageEmpty'):
            if self.thread_id:self.close_deleted()
            raise DeletedHistory('Входящее сообщение удалено. Пилот не включён.')
        if original.out or original.chat_id!=self.settings['peer_id']:
            raise ValueError('Не подтверждён отправитель входящего сообщения.')
        self.thread_id=self.store.enroll(company_id=self.settings['company_id'],business_id=self.policy.business_id,
            manager_id=self.manager_id,account_id=self.account_id,peer_id=self.settings['peer_id'],
            product=self.settings['product'],first_message_id=original.id,first_sent_at=int(original.date.timestamp()),anchor_kind='incoming')
        fresh=not self.store.thread(self.thread_id)['history_complete']
        new_incoming=await self.refresh(full=True,initial=fresh)
        self.store.mark_history_complete(self.thread_id)
        identity=await self.client.get_entity(self.peer)
        if identity.id!=self.settings['peer_id']:raise ValueError('Контакт Telegram изменился.')
        username=getattr(identity,'username',None) or ''
        if username and not re.fullmatch(r'[A-Za-z0-9_]{1,32}',username):raise ValueError('Некорректное имя Telegram.')
        with self.store.connection() as db:
            db.execute('UPDATE dialogue_threads SET peer_username=? WHERE id=? AND account_id=? AND peer_id=?',
                       (username,self.thread_id,self.account_id,self.settings['peer_id']))
        if self.store.unresolved_actions(self.thread_id):
            t=self.store.thread(self.thread_id)
            self.store.handoff(self.thread_id,event_key='interrupted_delivery',event_type='delivery',
                summary='Есть непроверенная отправка. Сначала проверьте её результат.',pause=True,expected_revision=t['revision'])
            return
        t=self.store.thread(self.thread_id)
        if not t['seller_enabled']:
            SellerControls(self.store).enable(self.thread_id,actor=self.account_id,manager=self.manager_id,revision=t['revision'])
        if self.settings.get('owner_test_grant') and self.settings.get('synthetic_test'):
            # The owner authorized this one new test. Never expose yesterday's fictional history.
            self.engine.analyst.test_context_start_id=original.id
            if fresh:
                t,history=self.store.snapshot(self.thread_id)
                if not any(m['direction']=='outgoing' and m['telegram_id']>original.id for m in history):
                    SellerControls(self.store).set_mode(self.thread_id,'active_negotiation',actor=self.account_id,
                        manager=self.manager_id,revision=t['revision'])
                    with self.store.connection() as db:
                        db.execute('UPDATE dialogue_threads SET control_reason=? WHERE id=?',
                            ('Автоответы подключены после подтверждённой первичной отправки. Владелец явно разрешил новый тест с Викой.',self.thread_id))
                    self.changed.set()
        # Existing manual pauses remain in force across restarts.
        if new_incoming and not fresh:self.changed.set()

    def close_deleted(self):
        thread=self.store.thread(self.thread_id)
        if thread['state'] not in {'closed','opt_out'}:
            self.store.set_state(self.thread_id,'closed',expected_revision=thread['revision'])

    async def on_deleted(self,event):
        peer=getattr(event,'chat_id',None)
        if peer is not None and peer!=self.settings['peer_id']:return
        deleted=set(event.deleted_ids)
        if deleted.intersection(m['telegram_id'] for m in self.store.snapshot(self.thread_id)[1]):
            self.close_deleted()

    async def verify_visible_history(self):
        """Recheck all known IDs before delivery, including deletions while AI was working."""
        if getattr(self,'settings',{}).get('stage3_verified_base'):
            if await self.refresh(full=True):self.changed.set()
        thread,history=self.store.snapshot(self.thread_id)
        ids=[m['telegram_id'] for m in history]
        if thread['first_message_id'] not in ids:
            self.close_deleted();return False
        visible=await self.client.get_messages(self.peer,ids=ids)
        found={m.id for m in visible if m is not None and type(m).__name__!='MessageEmpty'}
        if set(ids)-found:
            self.close_deleted();return False
        return True

    async def review_existing(self):
        """One review per authorized snapshot. Advanced conversations always stay manual."""
        key='dialogue_base_review:'+self.settings['review_id']+':'+str(self.settings['peer_id'])
        with self.store.connection() as db:
            if db.execute('SELECT 1 FROM local_settings WHERE key=?',(key,)).fetchone():return
        thread,history=self.store.snapshot(self.thread_id)
        if thread['state'] in {'closed','opt_out'}:return
        outgoing=[m for m in history if m['direction']=='outgoing' and m['telegram_id']!=thread['first_message_id']
                  and m['sent_at']>=thread['first_sent_at']]
        incoming=[m for m in history if m['direction']=='incoming']
        def manual(summary):
            current=self.store.thread(self.thread_id)
            self.store.handoff(self.thread_id,event_key=key,event_type='base_review',summary=summary,
                pause=True,expected_revision=current['revision'])
        if thread['state']=='human' or outgoing:
            manual('Нужно ручное рассмотрение: в переписке уже были дополнительные исходящие сообщения или диалог был передан менеджеру. Клиенту ничего не отправлено.')
        elif incoming:
            try:
                decision=await self.engine.analyst.analyze(thread=thread,history=history,now=int(time.time()))
                self.engine._check(self.thread_id,thread['revision'])
                if decision['intent']=='opt_out':
                    self.store.set_state(self.thread_id,'opt_out',expected_revision=thread['revision'])
                elif decision['intent']=='question' and incoming[-1]['sent_at']>=thread['first_sent_at']:
                    # The ordinary engine uses the complete context and verified KB,
                    # and persists a unique action before any send can occur.
                    await self.engine.on_incoming(self.thread_id)
                elif decision['intent']=='later' and type(decision.get('return_at')) is int and decision['return_at']>time.time():
                    self.store.set_state(self.thread_id,'later',expected_revision=thread['revision'],next_contact_at=decision['return_at'])
                else:manual('Нужно ручное рассмотрение существующего диалога: '+decision['summary'])
            except KnowledgeTransportUnavailable:
                raise
            except KnowledgeUnavailable:
                manual('Нужно ручное рассмотрение: не удалось надёжно разобрать существующую переписку. Клиенту ничего не отправлено.')
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO local_settings(key,value,updated_at) VALUES(?,?,?)',
                       (key,'done',datetime.now(timezone.utc).isoformat()))

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
        readable=False;preview=False
        if message.media:
            if getattr(self,'settings',{}).get('attachments_enabled') and not message.out:
                from dialogue_attachments import is_preview,observe
                preview=is_preview(message)
                readable=(not preview and int(message.date.timestamp())>=max(
                    self.settings['attachments_started_at'],self.store.thread(self.thread_id)['first_sent_at']))
                if readable:text=observe(self.store,self.thread_id,message)
                elif not preview:text+='\n[Вложение: содержимое автоматически не прочитано]'
            else:text+='\n[Вложение: содержимое автоматически не прочитано]'
        changed=self.store.record_message(self.thread_id,telegram_id=message.id,direction='outgoing' if message.out else 'incoming',
                                          text=text,sent_at=int(message.date.timestamp()),source=source)
        if changed and message.media and not message.out:
            thread=self.store.thread(self.thread_id)
            if thread['seller_enabled']:
                from dialogue_records import attachment
                attachment(self.store,self.thread_id,message)
            if readable or preview:return True
            if getattr(self,'settings',{}).get('attachments_enabled') and int(message.date.timestamp())<self.settings['attachments_started_at']:
                return True  # Historical attachments remain context; never bulk-download them.
            if int(message.date.timestamp())<thread['first_sent_at']:return not message.out
            self.store.handoff(self.thread_id,event_key='attachment:'+str(message.id),event_type='attachment',
                summary='Клиент прислал вложение. Нужно проверить его содержимое; автоответы приостановлены.',
                pause=True,expected_revision=thread['revision'])
        return changed and not message.out

    async def on_message(self,event):
        """Never wait for generation's lock to register a manager takeover.

        Telegram may echo our own outgoing message before send_message returns its
        ID. Quarantine those events during that one in-flight request; classify by
        the confirmed ID afterwards. A crash leaves the durable action unresolved.
        """
        if not event.is_private or event.chat_id!=self.settings['peer_id']:return
        from finance_priority import wait_external_receipt
        await wait_external_receipt(event.message)
        if event.message.out and getattr(self,'inflight',False):
            self.outgoing_buffer.append(event.message)
            return
        if self.record(event.message):
            self.latest_incoming_at=time.monotonic()
            self.changed.set()
        elif event.message.out and self.store.thread(self.thread_id)['seller_enabled']:
            self.changed.set()

    async def reconcile_resume(self):
        thread=self.store.thread(self.thread_id)
        if not thread['seller_enabled']:return
        if thread['mode']=='paused_until' and thread['paused_until'] is not None and thread['paused_until']<=self.store.clock():
            SellerControls(self.store).set_mode(self.thread_id,'active_negotiation',actor=self.account_id,
                manager=self.manager_id,revision=thread['revision'])
            thread=self.store.thread(self.thread_id)
        if not thread['resume_pending']:return
        epoch=thread['control_epoch']
        automatic=thread['control_reason'].startswith('Автоответы подключены после подтверждённой первичной отправки.')
        if not await self.verify_visible_history():return
        await self.refresh(full=True)
        if SellerControls(self.store).finish_resume(self.thread_id,epoch=epoch):
            self.changed.set()
        current=self.store.thread(self.thread_id)
        if automatic:
            with self.store.connection() as db:
                db.execute('UPDATE dialogue_threads SET control_reason=? WHERE id=? AND control_epoch=?',
                    ('Автоответы подключены после подтверждённой первичной отправки. Ожидаю ответ клиента.',self.thread_id,epoch))
            return  # Auto enrollment is not a manager click; do not send a misleading control notice.
        self.store.handoff(self.thread_id,event_key='control_resume:'+str(epoch),event_type='control',
            summary='Менеджер вернул диалог ИИ. Свежая переписка проверена; устаревшие ответы не повторяются.',
            pause=False,expected_revision=current['revision'])

    async def debounce(self):
        # Quiet period is bounded so a long stream cannot postpone processing forever.
        started=time.monotonic()
        while True:
            latest=getattr(self,'latest_incoming_at',None)
            if latest is None:
                await asyncio.sleep(3);return
            remaining=min(2-(time.monotonic()-latest),8-(time.monotonic()-started))
            if remaining<=0:return
            await asyncio.sleep(remaining)

    async def refresh(self,*,full=False,initial=False):
        minimum=0 if full else self.store.last_message_id(self.thread_id)
        incoming=False;seen=set()
        previous={m['telegram_id'] for m in self.store.snapshot(self.thread_id)[1]} if full else set()
        async for message in self.client.iter_messages(self.peer,reverse=True,min_id=minimum):
            from finance_priority import wait_external_receipt
            await wait_external_receipt(message)
            seen.add(message.id)
            incoming=self.record(message,full=initial) or incoming
        if full and previous-seen:self.close_deleted()
        return incoming

    async def send(self,action):
        import bot,access_runtime
        async def operation():
            if action['kind']=='calendar' and self.settings.get('finance_managed'):
                from finance_priority import calendar_allowed
                if not await calendar_allowed(self):return
            if not await self.verify_visible_history():return
            # Fetch messages arriving during the shared send interval before claiming.
            if await self.refresh():
                self.changed.set();return
            current=self.store.action_for_key(self.thread_id,action['action_key'])
            thread,_=self.store.snapshot(self.thread_id)
            allowed=(thread['state']=='opt_out' if action['kind']=='opt_out_ack' else thread['state'] in {'waiting','engaged','later'})
            if not current or current['status']!='pending' or not allowed:return
            if not can_send(thread,action['kind']):return
            self.store.claim_action(action['id'])
            self.inflight=True;self.outgoing_buffer=[]
            try:
                from forensic_trace import origin
                with origin('warmup' if action['kind']=='calendar' else 'auto_reply','dialogue_actions',action['id']):
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
            else:
                self.store.finish_action(action['id'],status='sent',telegram_id=sent.id)
                self.record(sent)
            finally:
                self.inflight=False
                for message in self.outgoing_buffer:self.record(message)
                self.outgoing_buffer=[]
        def guard():
            require_finance_access();bot.require_send_cooldown_finished()
        await self.gate.run(operation,guard)

    async def run(self):
        import bot,access_runtime
        try:
            await self.enroll()
            register_ui(self.application,self.store,account_id=self.account_id,manager_id=self.manager_id)
            thread=self.store.thread(self.thread_id)
            await self.application.bot.send_message(chat_id=self.account_id,
                text=f"Тестовый диалог подключён: {self.store.client_label(self.thread_id)}. Управление — /dialogues.",
                reply_markup=controls(self.thread_id,thread['revision'],thread['state'],thread=thread))
            handler=self.on_message
            self.event_handler=handler
            self.client.add_event_handler(handler,events.NewMessage(chats=[self.settings['peer_id']]))
            self.client.add_event_handler(handler,events.MessageEdited(chats=[self.settings['peer_id']]))
            self.delete_handler=self.on_deleted
            self.client.add_event_handler(self.delete_handler,events.MessageDeleted())
            # No reply to old history on startup. Only newly received messages trigger replies.
            while True:
                try:
                    async with self.lock:
                        require_finance_access();bot.require_send_cooldown_finished()
                        await self.reconcile_resume()
                        thread=self.store.thread(self.thread_id)
                        if thread['seller_enabled'] and thread['mode'] in {'off','paused_until'}:
                            await asyncio.sleep(1);continue
                        if await self.refresh():self.changed.set()
                    if self.changed.is_set():
                        await self.debounce()
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
                except KnowledgeTransportUnavailable as exc:
                    log.warning('Knowledge infrastructure unavailable (%s); retry in 60 seconds',str(exc))
                    await asyncio.sleep(60)
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
            if self.delete_handler:self.client.remove_event_handler(self.delete_handler)
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
        if settings.get('seller_stage1') and not account_allowed(self.account_id):
            raise PermissionError('Пилот продавца доступен только владельцу.')
        self.store=DialogueStore(self.root/config.DATABASE_PATH);self.store.initialize(sync=True)
        self.gate=DeliveryGate(interval=bot.SEND_INTERVAL_SECONDS)
        self.previous_gate=bot.delivery_gate;bot.delivery_gate=self.gate
        if settings.get('attachments_enabled'):
            from dialogue_attachments import initialize as initialize_attachments
            initialize_attachments(self.store,recover=True)
        if settings.get('stage3_calendar_enabled'):
            from dialogue_calendar import initialize as calendar_initialize,recover_and_reconcile
            calendar_initialize(self.store)
            with self.store.connection() as db:db.execute("INSERT OR REPLACE INTO seller_calendar_meta VALUES('live_enabled',?)",('true' if settings.get('stage3_live_enabled') else 'false',))
            with self.store.connection() as db:db.execute("INSERT OR REPLACE INTO seller_calendar_meta VALUES('test_peer_id',?)",(str(settings['stage3_test_peer_id']) if settings.get('stage3_test_peer_id') else '',))
            recover_and_reconcile(self.store,recover=True)
        self.workers={};self.attempted=set();self.scanned_at={};self.cursor=0
        self.handler=None;self.delete_handler=None;self.lock=asyncio.Lock();self.deleted_count=0

    async def add_contact(self,peer,product):
        settings={**self.settings,'peer_id':peer,'product':product}
        test=self.settings.get('owner_test_contact')
        if test and peer==test['peer_id']:
            settings.update(mode='pilot',anchor_kind='incoming',allowed_peer_ids=[peer],
                activated_at=test['activated_at'],synthetic_test=True,owner_test_grant=True)
        worker=DialogueRuntime(self.client,self.application,self.root,
            settings,self.policy,self.ai_client,gate=self.gate)
        try:await worker.enroll()
        except (AwaitingIncoming,DeletedHistory,AccessDenied,PeerFloodError,FloodWaitError,KnowledgeTransportUnavailable):raise
        except Exception:
            if worker.thread_id:
                thread=worker.store.thread(worker.thread_id)
                worker.store.handoff(worker.thread_id,event_key='history_not_verified',event_type='history',
                    summary='История не подтверждена полностью. Клиенту ничего не отправлено; нужно ручное рассмотрение.',
                    pause=True,expected_revision=thread['revision'])
            raise
        self.workers[peer]=worker;self.scanned_at[peer]=time.monotonic()
        if self.settings.get('anchor_kind')=='incoming':
            thread=self.store.thread(worker.thread_id)
            await self.application.bot.send_message(chat_id=self.account_id,
                text=dialogue_card(self.store,thread),
                reply_markup=controls(worker.thread_id,thread['revision'],thread['state'],thread=thread))
        return worker

    async def restore_audited_workers(self):
        """No startup crawl: final full history checks remain mandatory before decisions/sends."""
        from database import get_connection
        with get_connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='seller_history_audit'").fetchone():return
            rows=[dict(r) for r in db.execute("""SELECT t.* FROM seller_history_audit a JOIN dialogue_threads t ON t.id=a.thread_id
                WHERE a.verified=1 AND a.peer_id=t.peer_id AND a.first_message_id=t.first_message_id
                AND a.account_id=t.account_id AND a.company_id=t.company_id
                AND t.account_id=? AND t.manager_id=? AND t.company_id='dvk' AND t.business_id='dvk_finance'
                AND a.disposition IN ('conversation','silent','agreed')
                AND t.seller_enabled=1 AND t.history_complete=1 AND t.state NOT IN ('closed','opt_out')""",(self.account_id,self.manager_id))]
        with get_connection() as db:
            self.audited_blocked={r['peer_id']:dict(r) for r in db.execute("SELECT peer_id,thread_id,reason FROM seller_history_audit WHERE account_id=? AND disposition NOT IN ('conversation','silent','agreed')",(self.account_id,))}
        for t in rows:
            peer=t['peer_id']
            if peer in self.workers:continue
            worker=DialogueRuntime(self.client,self.application,self.root,
                {**self.settings,'peer_id':peer,'product':t['product']},self.policy,self.ai_client,gate=self.gate)
            worker.thread_id=t['id']
            try:worker.peer=await self.client.get_input_entity(peer)
            except (ValueError,TypeError):continue  # Normal discovery can resolve an unavailable peer safely.
            if self.store.unresolved_actions(t['id']):
                current=self.store.thread(t['id'])
                self.store.handoff(t['id'],event_key='audited_restore_uncertain',event_type='delivery',
                    summary='Есть непроверенная отправка. Проверьте её результат; ИИ приостановлен.',
                    pause=current['state'] not in {'closed','opt_out'},expected_revision=current['revision'])
            current=self.store.thread(t['id'])
            with self.store.connection() as db:
                latest=db.execute("SELECT MAX(telegram_id) FROM dialogue_messages WHERE thread_id=? AND direction='incoming'",(t['id'],)).fetchone()[0]
            if latest and latest>max(current['processed_incoming_id'],current['incoming_floor_id']):worker.changed.set()
            self.workers[peer]=worker;self.scanned_at[peer]=time.monotonic();self.attempted.add(peer)
            if len(self.workers)%25==0:await asyncio.sleep(0)
        self.attempted.update(getattr(self,'audited_blocked',{}))
        print('Проверенная финансовая база подключена: '+str(len(self.workers))+' диалогов.',flush=True)

    async def service(self,worker):
        import bot,access_runtime
        async with worker.lock:
            require_finance_access();bot.require_send_cooldown_finished()
            if getattr(self,'settings',{}).get('seller_stage1'):
                if self.settings.get('finance_managed'):
                    from finance_recovery import recover_calendar_failure
                    recover_calendar_failure(worker.store,worker.thread_id)
                await worker.reconcile_resume()
                thread=worker.store.thread(worker.thread_id)
                if thread['mode'] in {'off','paused_until'}:return
            peer=worker.settings['peer_id']
            # New/edit events wake this thread immediately. Reconcile gaps slowly,
            # instead of issuing hundreds of history requests every five seconds.
            if time.monotonic()-self.scanned_at[peer]>=300:
                if await worker.refresh():worker.changed.set()
                self.scanned_at[peer]=time.monotonic()
            if worker.changed.is_set():
                if getattr(self,'settings',{}).get('seller_stage1'):await worker.debounce()
                else:await asyncio.sleep(3)
                worker.changed.clear();await worker.refresh(full=bool(getattr(self,'settings',{}).get('stage3_verified_base')))
                if getattr(self,'settings',{}).get('attachments_enabled'):
                    from dialogue_attachments import process
                    if not await process(worker):return
                    # A new message or manual takeover during extraction invalidates this turn.
                    if await worker.refresh():worker.changed.set();return
                await worker.engine.on_incoming(worker.thread_id)
            if getattr(self,'settings',{}).get('stage3_calendar_enabled'):
                if self.settings.get('finance_managed'):
                    from finance_enrollment import seed_calendar
                    seed_calendar(worker)
                from dialogue_calendar import tick
                await tick(worker,self.settings)
            if not getattr(self,'settings',{}).get('reply_only'):
                await worker.engine.on_silence(worker.thread_id)
                await worker.engine.on_return(worker.thread_id)
            for action in worker.store.pending_actions(self.account_id):
                if action['thread_id']==worker.thread_id:await worker.send(action)
            if getattr(self,'settings',{}).get('stage3_verified_base'):
                from dialogue_calendar import resume_after_reply
                resume_after_reply(worker.store,worker.thread_id,self.settings)

    async def run(self):
        if getattr(self,'settings',{}).get('seller_stage1'):
            return await self.run_seller()
        import bot,access_runtime
        from database import get_connection
        last_discovery=0;pending=[];enrollment_blocked=False;reported=False;last_progress=0
        try:
            register_ui(self.application,self.store,account_id=self.account_id,manager_id=self.manager_id)
            async def handler(event):
                worker=self.workers.get(event.chat_id) if event.is_private else None
                if worker is None:return
                await worker.on_message(event)
            self.handler=handler
            # No username resolution for unknown senders, no reads of their history.
            self.client.add_event_handler(handler,events.NewMessage())
            self.client.add_event_handler(handler,events.MessageEdited())
            async def deleted(event):
                for worker in list(self.workers.values()):await worker.on_deleted(event)
            self.delete_handler=deleted
            self.client.add_event_handler(deleted,events.MessageDeleted())
            base=self.settings['mode']=='reviewed_base'
            unsupported=self.settings.get('unsupported_products',[]) if base else []
            await self.application.bot.send_message(chat_id=self.account_id,
                text=(f"Проверяю старую базу: {len(self.settings['contacts'])} клиентов по БГ и кредиту. Удалённым перепискам писать не буду. Продвинувшиеся диалоги передам вам. /dialogues — управление."
                      if base else f"Сопровождение включено только для выбранной очереди ({len(self.settings['prepared_ids'])} записей). Диалог подключается после подтверждённой отправки. Старая база не подключается. Управление — /dialogues.")
                     + ('\nДругие продукты требуют ручного рассмотрения, правил автоответов для них пока нет: '
                        + '; '.join(f"{v['product']}: {v['count']}" for v in unsupported) if unsupported else ''))
            while True:
                worker=None
                try:
                    require_finance_access();bot.require_send_cooldown_finished()
                    if not enrollment_blocked and time.monotonic()-last_discovery>=60:
                        if base:candidates=[(c['peer_id'],c['product']) for c in self.settings['contacts']]
                        else:
                            with get_connection() as db:candidates=eligible_contacts(db,self.manager_id,self.settings['prepared_ids'])
                        if self.settings.get('seller_stage1'):
                            candidates=[(p,product) for p,product in candidates if p in self.settings['allowed_peer_ids']]
                        pending=[item for item in candidates if item[0] not in self.attempted]
                        last_discovery=time.monotonic()
                    # Replies take priority over background enrollment and reminders.
                    worker=next((w for w in self.workers.values() if w.changed.is_set()),None)
                    if worker is None and pending and not enrollment_blocked:
                        peer,product=pending.pop(0);self.attempted.add(peer)
                        try:
                            await self.add_contact(peer,product)
                        except PeerFloodError:
                            enrollment_blocked=True;raise
                        except FloodWaitError:
                            # Retry the read-only enrollment only after Telegram's
                            # persisted cooldown, never while that pause is active.
                            self.attempted.discard(peer);last_discovery=0;raise
                        except AccessDenied:
                            self.attempted.discard(peer);raise
                        except KnowledgeTransportUnavailable:
                            self.attempted.discard(peer);last_discovery=0;raise
                        except DeletedHistory:
                            self.deleted_count+=1
                        except Exception as exc:
                            log.warning('Contact history enrollment deferred (%s)',type(exc).__name__)
                            await self.application.bot.send_message(chat_id=self.account_id,
                                text=f'Автообщение не включено для Telegram ID {peer}: история не подтверждена. Нужна проверка. Тип: {type(exc).__name__}.')
                    if worker is None and self.workers:
                        workers=list(self.workers.values());worker=workers[self.cursor%len(workers)];self.cursor+=1
                    if worker is not None:await self.service(worker)
                    if base and len(self.attempted)>=last_progress+25:
                        last_progress=len(self.attempted)
                        await self.application.bot.send_message(chat_id=self.account_id,
                            text=f"Проверка базы: {last_progress}/{len(self.settings['contacts'])}. Подтверждённых историй: {len(self.workers)}. Удалённых первых сообщений: {self.deleted_count}. Продолжаю постепенно.")
                    if self.attempted and not pending and not reported:
                        reported=True
                        await self.application.bot.send_message(chat_id=self.account_id,
                            text=(f'Проверка базы завершена. Проверено: {len(self.attempted)}. Диалогов с подтверждённой историей: {len(self.workers)}. Удалённое первое сообщение: {self.deleted_count} — отправок не будет. Других неподключённых: {len(self.attempted)-len(self.workers)-self.deleted_count}. Продвинувшиеся диалоги оставлены на ручном рассмотрении. /dialogues — управление.'
                                  if base else f'Сейчас подключено диалогов: {len(self.workers)}. Не подключено после проверки: {len(self.attempted)-len(self.workers)}. Остальные участники выбранной очереди подключатся после подтверждённой отправки. /dialogues — управление.'))
                except (AccessDenied,bot.TelegramSendPause,StaleDialogue):pass
                except PeerFloodError:
                    bot.pause_for_spam()
                except FloodWaitError as exc:
                    bot.pause_for_flood(exc)
                except KnowledgeTransportUnavailable as exc:
                    log.warning('Knowledge infrastructure unavailable (%s); retry in 60 seconds',str(exc))
                    await asyncio.sleep(60)
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
            if self.delete_handler:self.client.remove_event_handler(self.delete_handler)
            bot.delivery_gate=self.previous_gate
            await self.ai_client.close()

    async def run_seller(self):
        """Owner-only bounded concurrency; one task per chat, no background touches."""
        import bot,access_runtime
        from database import get_connection
        from dialogue_seller import event as audit_event
        running={};deferred={};last_discovery=0;waiting_incoming=({self.settings['owner_test_contact']['peer_id']} if self.settings.get('owner_test_contact') else set())
        async def handler(update):
            nonlocal last_discovery
            worker=self.workers.get(update.chat_id) if update.is_private else None
            if worker:await worker.on_message(update)
            elif (self.settings.get('mode')=='owner_outbound' and update.is_private and not update.message.out):
                with get_connection() as db:
                    eligible=seller_contacts(db,self.manager_id,self.settings)
                blocked=getattr(self,'audited_blocked',{}).get(update.chat_id)
                if blocked:
                    tid=blocked['thread_id'];thread=self.store.thread(tid)
                    if thread['mode']=='active_negotiation' and thread['resume_pending'] and thread['state'] not in {'closed','opt_out'}:
                        await self.add_contact(update.chat_id,thread['product']);self.attempted.add(update.chat_id)
                        self.audited_blocked.pop(update.chat_id,None)
                        await self.workers[update.chat_id].on_message(update)
                    else:
                        message=update.message
                        text=(message.raw_text or '')+('\n[Вложение: требуется ручной просмотр]' if message.media else '')
                        self.store.record_message(tid,telegram_id=message.id,direction='incoming',text=text,
                            sent_at=int(message.date.timestamp()),source='client')
                        thread=self.store.thread(tid)
                        self.store.handoff(tid,event_key='audited_incoming:'+str(message.id),event_type='copilot',
                            summary='Клиент написал, нужен ручной ответ. Причина прежней остановки: '+blocked['reason'][:250]+'\nСообщение: '+text[:600],
                            pause=False,expected_revision=thread['revision'])
                    return
                if any(peer==update.chat_id for peer,product in eligible):
                    waiting_incoming.add(update.chat_id);last_discovery=0
            elif (self.settings.get('anchor_kind')=='incoming' and update.is_private
                    and update.chat_id==self.settings['peer_id'] and not update.message.out):
                last_discovery=0
        async def deleted(update):
            for worker in list(self.workers.values()):await worker.on_deleted(update)
        async def service_one(worker):
            peer=worker.settings['peer_id'];started=time.monotonic()
            incoming_started=worker.latest_incoming_at
            finished=False
            retry_incoming=worker.changed.is_set()
            try:
                async with asyncio.timeout(120 if getattr(self,'settings',{}).get('attachments_enabled') else 50):
                    await self.service(worker)
                finished=True
            except (AccessDenied,bot.TelegramSendPause,StaleDialogue):
                deferred[peer]=time.monotonic()+5
            except PeerFloodError:
                bot.pause_for_spam()
            except FloodWaitError as exc:
                bot.pause_for_flood(exc)
            except KnowledgeTransportUnavailable:
                worker.changed.set();deferred[peer]=time.monotonic()+60
            except TRANSIENT_ERRORS as exc:
                # Transport failure does not transfer ownership or cancel the calendar.
                # Delivery uncertainty is handled by send() before this boundary.
                if retry_incoming:worker.changed.set()
                deferred[peer]=time.monotonic()+60
                record_retry(worker,exc)
            except Exception as exc:
                deferred[peer]=time.monotonic()+60
                thread=worker.store.thread(worker.thread_id)
                worker.store.handoff(worker.thread_id,event_key='seller_error:'+str(thread['revision']),event_type='runtime',
                    summary='Ответ остановлен: не удалось безопасно завершить обработку. Проверьте диалог. '
                            'Тип: '+type(exc).__name__,pause=thread['mode'] in {'active_negotiation','nurture','silent'},expected_revision=thread['revision'])
            finally:
                latest=incoming_started
                elapsed=time.monotonic()-(latest if latest is not None else started)
                thread=worker.store.thread(worker.thread_id)
                with worker.store.connection() as db:
                    audit_event(db,worker.thread_id,'processing_finished',thread['revision'],worker.store.clock(),
                                elapsed_ms=round(elapsed*1000),mode=thread['mode'],over_60s=elapsed>60)
                if latest is not None and elapsed>60 and thread['mode']=='active_negotiation':
                    worker.store.handoff(worker.thread_id,event_key='slow_reply:'+str(thread['revision']),event_type='latency',
                        summary='Ответ задержался более минуты. Проверьте диалог; при необходимости ответьте вручную.',
                        pause=False,expected_revision=thread['revision'])
                if finished and worker.latest_incoming_at==incoming_started:
                    worker.latest_incoming_at=None
        try:
            register_ui(self.application,self.store,account_id=self.account_id,manager_id=self.manager_id,seller_only=True)
            self.handler=handler;self.delete_handler=deleted
            self.client.add_event_handler(handler,events.NewMessage())
            self.client.add_event_handler(handler,events.MessageEdited())
            self.client.add_event_handler(deleted,events.MessageDeleted())
            if getattr(self,'settings',{}).get('stage3_verified_base'):
                await self.restore_audited_workers()
            if self.settings['mode']=='owner_outbound':
                await self.application.bot.send_message(chat_id=self.account_id,
                    text=('ИИ-продавец ДВК Финанс отслеживает новые ответы всей локальной финансовой базы по БГ и кредиту. ' if self.settings.get('watch_all_history') else 'ИИ-продавец ДВК Финанс включён для новых подтверждённых отправок по БГ и кредиту. ')+
                         'На ответы клиентов отвечает ИИ; ваш ручной ответ останавливает его в этом чате. '+
                         (('Прогрев проверенной базы включён по календарю. Управление — /dialogues.' if self.settings.get('stage3_live_enabled') else 'Исторические вопросы не переотправляются, напоминания молчащим выключены. Управление — /dialogues.') if self.settings.get('watch_all_history') else 'Старая база и напоминания молчащим не включены. Управление — /dialogues.'))
                (self.root/'data/seller-runtime-health.json').write_text(json.dumps({
                    'status':'started','account_id':self.account_id,'mode':self.settings['mode'],
                    'started_at':int(time.time()),'after_message_id':self.settings['after_message_id'],
                    'watch_all_history':self.settings.get('watch_all_history',False),'reply_only':self.settings.get('reply_only',False),
                    'attachments_enabled':self.settings.get('attachments_enabled',False),
                    'stage3_calendar_enabled':self.settings.get('stage3_calendar_enabled',False),'stage3_live_enabled':self.settings.get('stage3_live_enabled',False)
                }),encoding='utf-8')
                print('Автодиалоги ДВК Финанс включены для новых отправок по БГ и кредиту.',flush=True)
            elif not any(t['seller_enabled'] and t['peer_id']==self.settings.get('peer_id') for t in self.store.threads(self.account_id)):
                await self.application.bot.send_message(chat_id=self.account_id,
                    text='Ожидаю новый разрешённый тестовый диалог. До вашего явного возврата ИИ клиенту не пишет. '
                         'После подключения пришлю карточку с именем и кнопками.')
            while True:
                # Retrieve every task result; exceptions never become silent background failures.
                for peer,task in list(running.items()):
                    if task.done():
                        await task;del running[peer]
                try:
                    require_finance_access();bot.require_send_cooldown_finished()
                except (AccessDenied,bot.TelegramSendPause):
                    await asyncio.sleep(5);continue
                if time.monotonic()-last_discovery>=60:
                    if self.settings['mode']=='owner_outbound':
                        if self.settings.get('finance_managed'):
                            from finance_enrollment import restore_legacy_identities
                            if not hasattr(self, 'legacy_identity_attempted'):
                                self.legacy_identity_attempted = set()
                            await restore_legacy_identities(self.client, self.manager_id, self.legacy_identity_attempted)
                        with get_connection() as db:
                            candidates=seller_contacts(db,self.manager_id,self.settings)
                    elif self.settings['mode']=='pilot':
                        candidates=[(self.settings['peer_id'],self.settings['product'])]
                    else:
                        with get_connection() as db:
                            candidates=eligible_contacts(db,self.manager_id,self.settings['prepared_ids'])
                    ready_peers={self.store.thread(a['thread_id'])['peer_id']
                                 for a in self.store.pending_actions(self.account_id)}
                    candidates.sort(key=lambda item:(item[0] not in waiting_incoming,item[0] not in ready_peers))
                    for peer,product in candidates:
                        if (self.settings['mode']!='owner_outbound' and peer not in self.settings['allowed_peer_ids']) or peer in self.attempted:continue
                        try:
                            await self.add_contact(peer,product);self.attempted.add(peer);waiting_incoming.discard(peer)
                        except AwaitingIncoming:pass
                        except DeletedHistory:
                            self.attempted.add(peer)
                        except AccessDenied:pass
                        except PeerFloodError:bot.pause_for_spam()
                        except FloodWaitError as exc:bot.pause_for_flood(exc)
                        except Exception as exc:
                            log.warning('Seller enrollment deferred (%s)',type(exc).__name__)
                        break  # History discovery must not monopolize the reply workers.
                    backlog=any(peer not in self.attempted for peer,product in candidates)
                    last_discovery=time.monotonic()-(55 if backlog else 0)
                calendar_due=set()
                if self.settings.get('stage3_live_enabled'):
                    from dialogue_calendar import due_threads,recover_and_reconcile
                    if time.monotonic()-getattr(self,'calendar_last_reconciled',0)>30:
                        recover_and_reconcile(self.store);self.calendar_last_reconciled=time.monotonic()
                    calendar_due=due_threads(self.store)
                # Ready replies must not starve behind hundreds of background scans.
                ready_threads={a['thread_id'] for a in self.store.pending_actions(self.account_id)}
                threads={w.thread_id:self.store.thread(w.thread_id) for w in self.workers.values()}
                def priority(w):
                    t=threads[w.thread_id]
                    rank=(0 if w.changed.is_set() else 1 if w.thread_id in ready_threads else
                          2 if t['resume_pending'] else 3 if w.thread_id in calendar_due else 4)
                    return rank,self.scanned_at.get(w.settings['peer_id'],0)
                workers=sorted(self.workers.values(),key=priority)
                for worker in workers:
                    peer=worker.settings['peer_id'];thread=threads[worker.thread_id]
                    due_pause=thread['mode']=='paused_until' and thread['paused_until'] is not None and thread['paused_until']<=self.store.clock()
                    due_reply=worker.thread_id in ready_threads
                    needs_work=(thread['mode']!='off' and (worker.changed.is_set() or thread['resume_pending'] or due_pause or due_reply or worker.thread_id in calendar_due
                                or (thread['mode'] not in {'off','paused_until'} and time.monotonic()-self.scanned_at[peer]>=300))
                                and (thread['mode']!='paused_until' or due_pause))
                    if (len(running)<2 and peer not in running and needs_work
                            and time.monotonic()>=deferred.get(peer,0)):
                        running[peer]=asyncio.create_task(service_one(worker))
                try:await deliver_notices(self.application.bot,self.store,account_id=self.account_id)
                except Exception as exc:log.warning('Seller notice deferred (%s)',type(exc).__name__)
                await asyncio.sleep(.5)
        finally:
            for task in running.values():task.cancel()
            await asyncio.gather(*running.values(),return_exceptions=True)
            if self.handler:self.client.remove_event_handler(self.handler)
            if self.delete_handler:self.client.remove_event_handler(self.delete_handler)
            bot.delivery_gate=self.previous_gate
            await self.ai_client.close()


async def _start_enabled(client,application,root=Path('.')):
    settings=read_settings(root)
    if settings is None:return None
    import access_runtime
    if settings.get('seller_stage1'):
        me=await client.get_me()
        if not account_allowed(me.id) or access_runtime.account_id!=me.id:
            raise PermissionError('Пилот продавца доступен только подтверждённому аккаунту владельца.')
    if getattr(access_runtime,'business_ready',None) is not None:
        try:await asyncio.wait_for(access_runtime.business_ready.wait(),timeout=8)
        except asyncio.TimeoutError:pass
    grant=access_runtime._store.require_access()
    if grant.get('company_id') is not None and grant['company_id']!=settings['company_id']:
        raise AccessDenied('Компания в настройках диалогов не совпадает с назначением центра. Автодиалоги приостановлены.')
    if __import__('business_updates').verified_routes(access_runtime._store):
        from business_updates import load_policy
        policy=load_policy(root,access_runtime._store,settings['business_id'],Path(root)/'dialogue_profiles/dvk_finance.json')
    else:
        # Owner WORK uses stable 2.0.30; its private seller profile is validated below.
        # Do not require the unreleased company-package subsystem just to start it.
        policy=DialoguePolicy.load(Path(root)/'dialogue_profiles/dvk_finance.json')
    if (not getattr(policy, 'managed_package', False) and settings.get('seller_stage1')
            and (Path(root)/'data/seller-stage1-profile.json').exists()):
        trial=DialoguePolicy.load(Path(root)/'data/seller-stage1-profile.json')
        if trial.business_id!=settings['business_id'] or trial.model!=policy.model:
            raise PermissionError('Пилотный пакет не совпал с бизнесом или утверждённой моделью.')
        policy=trial
    if getattr(policy,'managed_package',False) and not policy.enabled:return None
    if not getattr(policy,'managed_package',False):
        policy.enabled=True  # Preserve the existing explicit private pilot, not managed company activation.
    import config
    from openai import AsyncOpenAI
    ai_client=AsyncOpenAI(api_key=config.OPENAI_API_KEY,timeout=60,max_retries=0)
    try:
        runtime_class=SelectedQueueRuntime if settings.get('seller_stage1') or settings['mode'] in {'selected_queue','reviewed_base'} else DialogueRuntime
        runtime=runtime_class(client,application,root,settings,policy,ai_client)
    except Exception:
        await ai_client.close()
        raise
    return asyncio.create_task(runtime.run(),name='finance-dialogue-runtime')




async def start(client, application, root=Path('.')):
    from finance_activation import supervise
    return asyncio.create_task(supervise(client, application, root, _start_enabled),
                               name='finance-activation')
