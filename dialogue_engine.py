from finance_scope import account_allowed, thread_allowed
"""Coordinate business decisions and persisted actions; sending is a separate worker.

Analyst, knowledge provider and writer are injected. All generation uses the
same complete-history snapshot; revisions reject results made stale by another
client message or a manager taking over.
"""
from datetime import datetime
import time
import uuid
import hashlib
import re
from dialogue_knowledge import KnowledgeUnavailable,KnowledgeTransportUnavailable
from dialogue_store import StaleDialogue

INTENTS={'opt_out','ready','need','call','documents','later','question','other','objection'}


class DialogueEngine:
    def __init__(self, store, policy, *, analyst, knowledge, writer, require_access, clock=time.time):
        self.store,self.policy=store,policy
        self.analyst,self.knowledge,self.writer=analyst,knowledge,writer
        self.require_access,self.clock=require_access,clock

    def _queue_action(self,thread_id,**kwargs):
        if self.policy.reply_style and self.store.thread(thread_id)['seller_enabled']:
            # Persist the delivery time; never sleep while holding the shared sender.
            kwargs['due_at']=int(self.clock())+min(500,max(8,round(len(kwargs['text'])/7)))
        return self.store.queue_action(thread_id,**kwargs)

    def _snapshot(self, thread_id):
        self.require_access()
        if not self.policy.enabled:
            raise PermissionError('Dialogue business package is not enabled')
        thread,history=self.store.snapshot(thread_id)
        if thread['seller_enabled'] and thread['active_product']:
            thread['product']=thread['active_product']
        if thread['business_id']!=self.policy.business_id or thread['product'] not in self.policy.products:
            raise PermissionError('Dialogue does not belong to the selected business/product')
        if not thread['history_complete']:
            raise StaleDialogue('Full history is not loaded')
        if self.policy.seller_memory and thread['seller_enabled']:
            from dialogue_memory import snapshot
            from dialogue_seller import OWNER_ACCOUNT_ID
            if not thread_allowed(thread):raise PermissionError('Память доступна только в пилоте владельца.')
            thread['memory']=snapshot(self.store,thread_id)
        if self.policy.seller_applications and thread['seller_enabled']:
            from dialogue_applications import applications
            from dialogue_seller import OWNER_ACCOUNT_ID
            if not thread_allowed(thread):raise PermissionError('Заявки доступны только владельцу.')
            thread['applications']=applications(self.store,thread_id)
        if thread['seller_enabled']:
            from dialogue_initial_research import load
            thread['initial_research']=load(self.store,thread)
        return thread,history

    def _decision_scope(self,thread,history,decision):
        """Validate product routing and a global contact stop independently of prose."""
        if not thread['seller_enabled']:return thread,decision
        product=decision.get('requested_product')
        if product is not None:
            if product not in self.policy.products:raise ValueError('Unsupported requested product')
            if product!=thread['product']:
                with self.store.connection() as db:
                    changed=db.execute('UPDATE dialogue_threads SET active_product=?,revision=revision+1,updated_at=? '
                        'WHERE id=? AND revision=?',(product,int(self.clock()),thread['id'],thread['revision'])).rowcount
                    if changed!=1:raise StaleDialogue('Conversation changed during product selection')
                    db.execute("UPDATE dialogue_actions SET status='cancelled' WHERE thread_id=? AND status='pending'",(thread['id'],))
                thread,_=self._snapshot(thread['id'])
        if decision.get('intent')=='opt_out' and self.policy.contact_stop_phrases:
            latest=next((m['text'] for m in reversed(history) if m['direction']=='incoming'),'')
            normalized=' '.join(latest.casefold().split())
            evidence=decision.get('contact_stop_evidence')
            quote=' '.join(evidence.casefold().split()) if isinstance(evidence,str) else ''
            # The model cannot turn a product rejection into a permanent contact ban.
            if (not quote or quote not in normalized
                    or not any(phrase.casefold() in quote for phrase in self.policy.contact_stop_phrases)):
                decision={**decision,'intent':'other','knowledge_question':latest}
        normalized=lambda s:re.sub(r'[\W_]+',' ',s.casefold()).strip()
        latest=next((m['text'] for m in reversed(history) if m['direction']=='incoming'),'')
        if any(' '+normalized(p)+' ' in ' '+normalized(latest)+' ' for p in self.policy.contact_stop_phrases):
            decision={**decision,'intent':'opt_out','contact_stop_evidence':latest,'application':None}
        known_need=any(a['status']=='open' and a['quality']=='valid' for a in thread.get('applications',[]))
        recent=[m['text'] for m in history if m['direction']=='incoming'][-2:]
        if decision.get('intent')=='ready' and '?' in latest and any(
                normalized(p) in normalized(s) for p in self.policy.exploration_phrases for s in recent):
            decision={**decision,'intent':'question','reply_required':True}
        declined=lambda text:any(' '+normalized(p)+' ' in ' '+normalized(text)+' '
            for p in self.policy.declined_answers)
        if decision.get('intent')!='opt_out' and declined(latest):
            # A commercial objection is actionable before any need/application is established.
            attempts=0
            for previous in history:
                if previous['direction']!='incoming' or previous['telegram_id']==history[-1]['telegram_id'] or not declined(previous['text']):continue
                with self.store.connection() as db:
                    answered=db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status='sent' "
                        "AND action_key LIKE ?",(thread['id'],'%reply:'+str(previous['telegram_id'])+':%')).fetchone()
                attempts+=bool(answered)
            if attempts>=2:
                decision={**decision,'intent':'other','reply_required':False,'application':None,
                    'summary':decision['summary']+' Две попытки отработки отказа уже сделаны; дальнейший дожим прекращён по методике.'}
            else:
                decision={**decision,'intent':'objection','reply_required':True,'needs_knowledge':False,
                    'knowledge_question':'','application':None}
                decision['required_question_focus']='Отработать текущее возражение по основной методике продаж: учесть весь контекст, понять реальную причину отказа одним уместным вопросом. Не повторять отвеченное, не спорить, не читать презентацию и не требовать сразу сумму кредита.'
        if (decision.get('intent')!='opt_out' and known_need and self.policy.service_objection_question
                and any(normalized(p) in normalized(s) for p in self.policy.service_objection_phrases for s in [latest])):
            decision={**decision,'intent':'objection','reply_required':True,
                'knowledge_question':self.policy.service_objection_question,'application':None}
        if decision.get('intent')=='objection':
            # An objection is not a new application/qualification event. Never
            # block its response on the analyst's repeated application proposal.
            decision={**decision,'application':None,'reply_required':True}
        if self.policy.email_contact_phrases and decision.get('intent')!='opt_out':
            customer_messages=[m['text'] for m in history if m['direction']=='incoming']
            email_pattern=r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}'
            has_email=any(re.search(email_pattern,s,re.I) for s in customer_messages)
            email_request=any(normalized(p) in normalized(latest) for p in self.policy.email_contact_phrases)
            call_request=any(p.casefold() in latest.casefold() for p in self.policy.call_request_phrases)
            if email_request and not has_email and not call_request:
                decision={**decision,'intent':'question','reply_required':True,'needs_knowledge':False,
                    'knowledge_question':'','application':None,
                    'required_question_focus':'Попросить адрес почты бухгалтера. Ничего ещё не отправлено; не заявлять обратного'}
                if decision.get('sales_plan'):
                    decision['sales_plan']={**decision['sales_plan'],'move':'diagnose','case_topic':None,'case_question':'',
                        'next_step':decision['required_question_focus']}
            elif re.search(email_pattern,latest,re.I) and not call_request:
                preceding=next((m['text'] for m in reversed(history) if m['direction']=='outgoing'),'')
                if 'почт' in preceding.casefold():
                    decision={**decision,'intent':'question','reply_required':True,'needs_knowledge':False,
                        'knowledge_question':'','application':None}
        if self.policy.complete_agreements:
            from dialogue_agreements import route
            decision=route(decision,history,self.policy)
        return thread,decision

    def _check(self, thread_id, revision):
        self.require_access()
        current,_=self.store.snapshot(thread_id)
        if current['revision']!=revision:
            raise StaleDialogue('Conversation changed during generation')

    def _handoff(self, thread, event, summary, event_key):
        if event=='knowledge_missing':event_key+=':revision:'+str(thread['revision'])
        rule=self.policy.handoff(event)
        if event in {'need','call','documents'} and self.store.already_notified(thread['id'],event) and not rule.pause:
            return None
        if not rule.notify:
            if rule.pause:self.store.set_state(thread['id'],'human',expected_revision=thread['revision'])
            return None
        return self.store.handoff(thread['id'],event_key=event_key,event_type=event,
                                  summary=summary,pause=rule.pause,expected_revision=thread['revision'],reason=rule.label)

    async def on_incoming(self, thread_id):
        thread,history=self._snapshot(thread_id)
        if not thread['seller_enabled']:
            return await self._on_incoming(thread_id)
        if thread['mode'] in {'off','paused_until'} or thread['resume_pending'] or thread['state'] in {'opt_out','closed'}:
            return None
        if thread['mode']=='copilot':
            if thread['copilot_revision']==thread['revision']:return None
            decision=await self.analyst.analyze(thread=thread,history=history,now=int(self.clock()))
            self._check(thread_id,thread['revision'])
            if not isinstance(decision,dict) or decision.get('intent') not in INTENTS:
                raise ValueError('Invalid copilot decision')
            summary=decision.get('summary')
            if not isinstance(summary,str) or not summary.strip() or len(summary)>2000:
                raise ValueError('Invalid copilot summary')
            thread,decision=self._decision_scope(thread,history,decision)
            if decision['intent']=='opt_out':
                self.store.set_state(thread_id,'opt_out',expected_revision=thread['revision'])
                return None  # Copilot never sends even an apology.
            from dialogue_seller import event
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if self.store._thread(db,thread_id)['revision']!=thread['revision']:
                    raise StaleDialogue('Диалог изменился во время анализа помощника.')
                db.execute('UPDATE dialogue_threads SET copilot_revision=? WHERE id=?',(thread['revision'],thread_id))
                event(db,thread_id,'copilot_analysis',thread['revision'],self.clock(),summary=summary,intent=decision['intent'])
            if decision['intent'] in {'ready','need','call','documents'} and not self.store.already_notified(thread_id,'copilot'):
                self.store.handoff(thread_id,event_key='copilot:'+str(thread['revision']),event_type='copilot',
                    summary=summary,pause=False,expected_revision=thread['revision'])
            return None
        incoming=[m for m in history if m['direction']=='incoming' and m['sent_at']>=thread['first_sent_at']]
        if not incoming or incoming[-1]['telegram_id']<=max(thread['processed_incoming_id'],thread.get('incoming_floor_id',0)):
            return None
        normalized=lambda s:re.sub(r'[\W_]+',' ',s.casefold()).strip()
        if self.policy.reply_style and re.search(r'\b(?:вы|ты|это)\s+(?:(?:точно|случайно|вообще|не)\s+)*(?:бот|робот|ии|нейросеть|автомат)|кто\s+(?:мне\s+)?отвечает',incoming[-1]['text'],re.I):
            self.store.handoff(thread_id,event_key='identity:'+str(incoming[-1]['telegram_id']),
                event_type='identity_question',summary='Клиент спрашивает, кто отвечает. Автоответы остановлены без сообщения клиенту. Ответьте лично. Реплика: '+incoming[-1]['text'][:1000],
                pause=True,expected_revision=thread['revision'],reason='Клиент спросил, общается ли он с ботом. Нужен личный ответ менеджера.')
            return None
        preceding=next((m for m in reversed(history) if m['direction']=='outgoing' and m['telegram_id']<incoming[-1]['telegram_id']),None)
        matched=next(((name,rule) for name,rule in [('role_ack',self.policy.role_ack),('need_ack',self.policy.need_ack)]
                      if rule is not None and normalized(incoming[-1]['text']) in {normalized(s) for s in rule['answers']}
                      and preceding is not None and any(normalized(q) in normalized(preceding['text']) for q in rule['questions'])),None)
        if getattr(self.analyst,'primary_methodology',()):matched=None
        if matched:
            # This policy-defined question asserts no bank conditions. It does
            # not need a remote knowledge request or create a lead/handoff.
            name,rule=matched
            key=name+':'+str(incoming[-1]['telegram_id'])
            previous=self.store.action_for_key(thread_id,key)
            if previous and previous['status'] in {'cancelled','not_sent'} and thread['resume_turn_id']==incoming[-1]['telegram_id']:
                key+=':return:'+str(thread['control_epoch'])
            action=None if self.store.action_for_key(thread_id,key) else self._queue_action(thread_id,
                action_key=key,kind='reply',text=rule['reply'],expected_revision=thread['revision'],due_at=int(self.clock()))
        else:action=await self._on_incoming(thread_id)
        with self.store.connection() as db:
            db.execute('UPDATE dialogue_threads SET processed_incoming_id=MAX(processed_incoming_id,?) WHERE id=?',
                       (incoming[-1]['telegram_id'],thread_id))
        return action

    async def _on_incoming(self, thread_id):
        thread,history=self._snapshot(thread_id)
        if thread['state'] in {'human','opt_out','closed'}:
            return None
        incoming=[m for m in history if m['direction']=='incoming' and m['sent_at']>=thread['first_sent_at']]
        if not incoming:
            return None
        digest=hashlib.sha256(incoming[-1]['text'].encode('utf-8')).hexdigest()[:12]
        key='reply:'+str(incoming[-1]['telegram_id'])+':'+digest
        previous=self.store.action_for_key(thread_id,key)
        if (thread['seller_enabled'] and thread['resume_turn_id']==incoming[-1]['telegram_id']
                and previous and previous['status'] in {'cancelled','not_sent'}):
            key+=':return:'+str(thread['control_epoch'])
        if self.store.action_for_key(thread_id,key):
            return None
        try:
            decision=await self.analyst.analyze(thread=thread,history=history,now=int(self.clock()))
        except KnowledgeTransportUnavailable:
            raise
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','Не удалось надёжно разобрать ответ клиента. Нужна помощь менеджера.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        if (not isinstance(decision,dict) or decision.get('intent') not in INTENTS
                or not isinstance(decision.get('summary'),str) or not decision['summary'].strip()
                or len(decision['summary'])>2000):
            raise ValueError('Invalid dialogue decision')
        intent=decision['intent']
        thread,decision=self._decision_scope(thread,history,decision)
        if thread['seller_enabled']:
            from dialogue_seller import event
            with self.store.connection() as db:
                event(db,thread_id,'incoming_decision',thread['revision'],self.clock(),
                      message_id=incoming[-1]['telegram_id'],intent=decision['intent'],
                      initial_research=[{k:r[k] for k in ('research_run_id','message_id','sha256')} for r in thread.get('initial_research',[])],
                      reply_required=decision.get('reply_required'),summary=decision['summary'],
                      methodology_sources=decision.get('methodology_sources',[]),
                      required_question_focus=decision.get('required_question_focus'),
                      communication_plan=decision.get('communication_plan'))
        if self.policy.seller_memory and thread['seller_enabled'] and decision['intent'] not in {'opt_out','objection'} and 'memory_facts' in decision:
            from dialogue_memory import save,snapshot
            try:save(self.store,thread,history,decision['memory_facts'])
            except StaleDialogue:raise
            except ValueError as exc:
                # Reject the proposed mutation, not the whole sales conversation.
                from dialogue_seller import event
                with self.store.connection() as db:
                    event(db,thread_id,'memory_validation_failed',thread['revision'],self.clock(),reason=str(exc))
            thread['memory']=snapshot(self.store,thread_id)
        intent=decision['intent']
        if intent=='opt_out':
            self.store.set_state(thread_id,'opt_out',expected_revision=thread['revision'])
            revision=self.store.snapshot(thread_id)[0]['revision']
            return self._queue_action(thread_id,action_key=key,kind='opt_out_ack',text=self.policy.apology,
                                           expected_revision=revision,due_at=int(self.clock()))
        if self.policy.seller_applications and thread['seller_enabled'] and decision.get('application') is not None:
            from dialogue_applications import record,applications
            try:
                record(self.store,thread,history,decision['application'],products=self.policy.products,
                       turn_id=incoming[-1]['telegram_id'],review_epoch=thread['control_epoch'])
            except StaleDialogue:raise
            except ValueError as exc:
                from dialogue_seller import event
                with self.store.connection() as db:
                    event(db,thread_id,'application_validation_failed',thread['revision'],self.clock(),reason=str(exc))
                # An unverified proposal cannot alter an application/KPI. The
                # reply can still rely on the original conversation and facts.
            thread['applications']=applications(self.store,thread_id)
        if decision.get('communication_complete'):
            from dialogue_agreements import summary
            self.store.handoff(thread_id,event_key='communication:'+key,event_type='communication_task',
                summary=summary(decision['communication_complete']),pause=True,expected_revision=thread['revision'],
                reason='Согласовано, с кем, как, когда и по какому вопросу связаться. Действие ещё не выполнено.')
            if getattr(self,'calendar_settings',{}).get('stage3_calendar_enabled'):
                from dialogue_calendar import external_agreement
                external_agreement(self.store,thread_id,history,decision['communication_complete'])
            return None
        if intent=='ready':
            self._handoff(thread,'ready',decision['summary'],'ready:'+key)
            return None
        if intent in {'need','call','documents'}:
            self._handoff(thread,intent,decision['summary'],intent+':'+key)
            if self.store.thread(thread_id)['state'] in {'human','opt_out','closed'}:
                return None  # Handoff is the send boundary, not merely a notice.
        latest_question=incoming[-1]['text'].casefold()
        factual_question=('?' in latest_question and any(p.casefold() in latest_question for p in self.policy.knowledge_required_phrases))
        if (thread['seller_enabled'] and self.policy.conversation_basis and decision.get('needs_knowledge') is False
                and decision.get('reply_required') is not False and not factual_question and intent not in {'opt_out','later'}
                and not (intent=='objection' and decision.get('knowledge_question'))):
            from dialogue_knowledge import KnowledgeAnswer
            try:
                text=await self.writer.compose(thread=thread,history=history,
                    decision={**decision,'response_scope':'discovery'},
                    knowledge=KnowledgeAnswer(self.policy.conversation_basis,(),uuid.uuid4().hex),stage=None)
            except KnowledgeUnavailable:
                self._check(thread_id,thread['revision'])
                self._handoff(thread,'knowledge_missing','Не удалось надёжно сформировать следующий вопрос. Нужна помощь менеджера.','discovery_failed:'+key)
                return None
            self._check(thread_id,thread['revision'])
            return self._queue_action(thread_id,action_key=key,kind='reply',text=text,expected_revision=thread['revision'],due_at=int(self.clock()))
        if (thread['seller_enabled'] and self.policy.case_learning and decision.get('sales_plan')
                and decision.get('reply_required') is not False):
            from dialogue_case_learning import retrieve
            thread['case_insights']=await retrieve(self.store,thread,decision['sales_plan'],self.policy,self.knowledge,
                lambda:self._check(thread_id,thread['revision']))
            from dialogue_seller import event
            with self.store.connection() as db:
                event(db,thread_id,'sales_move_planned',thread['revision'],self.clock(),
                    message_id=incoming[-1]['telegram_id'],plan=decision['sales_plan'],
                    case_sources=[s['file_id'] for c in thread['case_insights'] for s in c['sources']])
        if intent=='objection' and not decision.get('knowledge_question'):
            from dialogue_knowledge import KnowledgeAnswer
            try:
                text=await self.writer.compose(thread=thread,history=history,decision=decision,
                    knowledge=KnowledgeAnswer('Только уточнить причину возражения; подтверждённых новых банковских условий нет.',(),uuid.uuid4().hex),stage=None)
            except KnowledgeUnavailable:
                self._check(thread_id,thread['revision'])
                self._handoff(thread,'knowledge_missing','Не удалось корректно уточнить возражение. Нужен личный ответ.','objection_missing:'+key)
                return None
            self._check(thread_id,thread['revision'])
            return self._queue_action(thread_id,action_key=key,kind='reply',text=text,expected_revision=thread['revision'],due_at=int(self.clock()))
        if thread['seller_enabled'] and decision.get('reply_required') is False:
            return None  # Notifications and client replies are separate decisions.
        if intent=='later' and getattr(self,'calendar_settings',{}).get('stage3_calendar_enabled'):
            from dialogue_calendar import parse_when,agreed,label_time
            latest=incoming[-1];words=latest['text'];proof={}
            if re.fullmatch(r'\s*(?:да|верно|договорились|подтверждаю)[.!\s]*',words,re.I):
                previous=next((m for m in reversed(history) if m['direction']=='outgoing' and m['telegram_id']<latest['telegram_id']),None)
                if previous:
                    words=previous['text'];proof={'proposal_message_id':previous['telegram_id'],'proposal_text':words}
            try:when=parse_when(words,self.clock())
            except ValueError:
                when=None
            if when is None:
                text='Когда вам удобно вернуться к вопросу? Напишите дату и, если важно, время с часовым поясом.'
                self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
            else:
                agreed(self.store,thread_id,source_id=latest['telegram_id'],quote=latest['text'],due=when,
                    purpose=decision['summary'],live=self.calendar_settings.get('stage3_live_enabled',False),proof=proof)
                self.store.set_state(thread_id,'later',expected_revision=thread['revision'],next_contact_at=when)
                text='Хорошо, договорились на '+label_time(when)+'.'
            revision=self.store.thread(thread_id)['revision']
            return self._queue_action(thread_id,action_key=key,kind='reply',text=text,expected_revision=revision,due_at=int(self.clock()))
        if intent=='later':
            when=decision.get('return_at')
            if type(when) is not int or when<=self.clock():
                # Ask a concrete date instead of inventing one or scheduling a past request.
                text='Подскажите, когда будет удобно вернуться к этому вопросу?'
                self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
            else:
                self.store.set_state(thread_id,'later',expected_revision=thread['revision'],next_contact_at=when)
                date=datetime.fromtimestamp(when,self.policy.timezone).strftime('%d.%m.%Y')
                text=f'Хорошо, вернусь к вопросу {date}.'
            revision=self.store.snapshot(thread_id)[0]['revision']
            return self._queue_action(thread_id,action_key=key,kind='reply',text=text,
                                           expected_revision=revision,due_at=int(self.clock()))
        question=decision.get('knowledge_question')
        if intent in self.policy.acknowledgements and (not question or not question.strip()):
            if thread['seller_enabled'] and self.store.handoff_acknowledged(thread_id,intent,self.policy.acknowledgements[intent]):
                # Even a classifier regression cannot repeat the template. New
                # questions take the normal knowledge path; an unclear follow-up
                # is handed to the manager instead of guessing at the complaint.
                self.store.handoff(thread_id,event_key='ack_followup:'+key,event_type='handoff_followup',
                    summary='Клиент уже получил подтверждение передачи. Появилась новая реплика, но ответ надёжно не определён. '
                            'Повторное подтверждение не отправлено; проверьте диалог.',
                    pause=True,expected_revision=thread['revision'])
                return None
            self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
            revision=self.store.thread(thread_id)['revision']
            ack_key='ack:'+intent+':'+key if thread['seller_enabled'] else key
            return self._queue_action(thread_id,action_key=ack_key,kind='reply',text=self.policy.acknowledgements[intent],
                                           expected_revision=revision,due_at=int(self.clock()))
        if not isinstance(question,str) or not question.strip() or len(question)>6000:
            self._handoff(thread,'knowledge_missing','Не удалось сформулировать надёжный запрос к базе знаний.','knowledge_missing:'+key)
            return None
        try:
            knowledge=await self.knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                                                  question=question,request_id=uuid.uuid4().hex)
            self._check(thread_id,thread['revision'])
            source_text=' '.join(e.text for e in getattr(knowledge,'evidence',())).casefold()
            if thread['seller_enabled'] and any(p.casefold() in source_text for p in self.policy.restricted_knowledge_markers):
                from dialogue_knowledge import public_document_answer
                public=public_document_answer(knowledge,self.policy,question)
                if public is None:
                    self.store.handoff(thread_id,event_key='restricted_knowledge:'+key,event_type='restricted_knowledge',
                        summary='Найденные условия содержат внутреннюю пометку или требуют уточнения руководителем. '
                                'Клиенту они не отправлены. Нужно лично проверить применимость и допустимость предложения.',
                        pause=True,expected_revision=thread['revision'],reason='Условия требуют подтверждения руководителем.')
                    return None
                knowledge=public
            if thread['seller_enabled']:
                from dialogue_seller import event
                with self.store.connection() as db:
                    event(db,thread_id,'knowledge_evidence',thread['revision'],self.clock(),product=thread['product'],
                        request_id=getattr(knowledge,'request_id',None),question=question,answer=knowledge.answer,
                        sources=[dict(file_id=e.file_id,filename=e.filename,text=e.text) for e in getattr(knowledge,'evidence',())])
            text=await self.writer.compose(thread=thread,history=history,decision=decision,knowledge=knowledge,stage=None)
        except KnowledgeTransportUnavailable:
            raise
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','В базе знаний не найден надёжный ответ. Нужна помощь менеджера.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
        revision=self.store.snapshot(thread_id)[0]['revision']
        return self._queue_action(thread_id,action_key=key,kind='reply',text=text,
                                       expected_revision=revision,due_at=int(self.clock()))

    async def on_silence(self, thread_id):
        thread,history=self._snapshot(thread_id)
        if thread['seller_enabled']:return None  # New strategies require stage 3 approval.
        if thread['state']!='waiting':
            return None
        answered=any(m['direction']=='incoming' and m['sent_at']>=thread['first_sent_at'] for m in history)
        sent=self.store.reminder_history(thread_id)
        due=self.policy.next_reminder_at(thread['first_sent_at'],sent,has_reply=answered)
        if due is None or due>self.clock():
            return None
        stage=len(sent)+1
        key='reminder:'+str(stage)
        if self.store.action_for_key(thread_id,key):
            return None
        try:
            knowledge=await self.knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                                                  question='Какие подтверждённые возможности ДВК Финанс по этому продукту полезны клиенту?',request_id=uuid.uuid4().hex)
            self._check(thread_id,thread['revision'])
            text=await self.writer.compose(thread=thread,history=history,decision=None,knowledge=knowledge,stage=stage)
        except KnowledgeTransportUnavailable:
            raise
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','Не удалось подготовить напоминание на основе базы знаний.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        return self._queue_action(thread_id,action_key=key,kind='reminder',text=text,
                                       expected_revision=thread['revision'],due_at=int(self.clock()))

    async def on_return(self,thread_id):
        thread,history=self._snapshot(thread_id)
        if thread['seller_enabled']:return None
        when=thread['next_contact_at']
        if thread['state']!='later' or when is None or when>self.clock():return None
        key='return:'+str(when)
        if self.store.action_for_key(thread_id,key):return None
        try:
            knowledge=await self.knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                question='Какие подтверждённые возможности по продукту полезны при возобновлении обсуждения?',request_id=uuid.uuid4().hex)
            self._check(thread_id,thread['revision'])
            text=await self.writer.compose(thread=thread,history=history,decision=None,knowledge=knowledge,stage='agreed_return')
        except KnowledgeTransportUnavailable:
            raise
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','Не удалось подготовить согласованное возвращение к клиенту.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
        revision=self.store.snapshot(thread_id)[0]['revision']
        return self._queue_action(thread_id,action_key=key,kind='reply',text=text,expected_revision=revision,due_at=int(self.clock()))
