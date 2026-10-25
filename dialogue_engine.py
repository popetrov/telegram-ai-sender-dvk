"""Coordinate business decisions and persisted actions; sending is a separate worker.

Analyst, knowledge provider and writer are injected. All generation uses the
same complete-history snapshot; revisions reject results made stale by another
client message or a manager taking over.
"""
from datetime import datetime
import time
import uuid
import hashlib
from dialogue_knowledge import KnowledgeUnavailable
from dialogue_store import StaleDialogue

INTENTS={'opt_out','ready','need','call','documents','later','question','other'}


class DialogueEngine:
    def __init__(self, store, policy, *, analyst, knowledge, writer, require_access, clock=time.time):
        self.store,self.policy=store,policy
        self.analyst,self.knowledge,self.writer=analyst,knowledge,writer
        self.require_access,self.clock=require_access,clock

    def _snapshot(self, thread_id):
        self.require_access()
        if not self.policy.enabled:
            raise PermissionError('Dialogue business package is not enabled')
        thread,history=self.store.snapshot(thread_id)
        if thread['business_id']!=self.policy.business_id or thread['product'] not in self.policy.products:
            raise PermissionError('Dialogue does not belong to the selected business/product')
        if not thread['history_complete']:
            raise StaleDialogue('Full history is not loaded')
        return thread,history

    def _check(self, thread_id, revision):
        self.require_access()
        current,_=self.store.snapshot(thread_id)
        if current['revision']!=revision:
            raise StaleDialogue('Conversation changed during generation')

    def _handoff(self, thread, event, summary, event_key):
        rule=self.policy.handoff(event)
        if event in {'need','call','documents'} and self.store.already_notified(thread['id'],event):
            return None
        if not rule.notify:
            if rule.pause:self.store.set_state(thread['id'],'human',expected_revision=thread['revision'])
            return None
        return self.store.handoff(thread['id'],event_key=event_key,event_type=event,
                                  summary=summary,pause=rule.pause,expected_revision=thread['revision'])

    async def on_incoming(self, thread_id):
        thread,history=self._snapshot(thread_id)
        if thread['state'] in {'human','opt_out','closed'}:
            return None
        incoming=[m for m in history if m['direction']=='incoming' and m['sent_at']>=thread['first_sent_at']]
        if not incoming:
            return None
        digest=hashlib.sha256(incoming[-1]['text'].encode('utf-8')).hexdigest()[:12]
        key='reply:'+str(incoming[-1]['telegram_id'])+':'+digest
        if self.store.action_for_key(thread_id,key):
            return None
        try:
            decision=await self.analyst.analyze(thread=thread,history=history,now=int(self.clock()))
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
        if intent=='opt_out':
            self.store.set_state(thread_id,'opt_out',expected_revision=thread['revision'])
            revision=self.store.snapshot(thread_id)[0]['revision']
            return self.store.queue_action(thread_id,action_key=key,kind='opt_out_ack',text=self.policy.apology,
                                           expected_revision=revision,due_at=int(self.clock()))
        if intent=='ready':
            self._handoff(thread,'ready',decision['summary'],'ready:'+key)
            return None
        if intent in {'need','call','documents'}:
            self._handoff(thread,intent,decision['summary'],intent+':'+key)
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
            return self.store.queue_action(thread_id,action_key=key,kind='reply',text=text,
                                           expected_revision=revision,due_at=int(self.clock()))
        question=decision.get('knowledge_question')
        if intent in self.policy.acknowledgements and (not question or not question.strip()):
            self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
            revision=self.store.thread(thread_id)['revision']
            return self.store.queue_action(thread_id,action_key=key,kind='reply',text=self.policy.acknowledgements[intent],
                                           expected_revision=revision,due_at=int(self.clock()))
        if not isinstance(question,str) or not question.strip() or len(question)>6000:
            self._handoff(thread,'knowledge_missing','Не удалось сформулировать надёжный запрос к базе знаний.','knowledge_missing:'+key)
            return None
        try:
            knowledge=await self.knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                                                  question=question,request_id=uuid.uuid4().hex)
            self._check(thread_id,thread['revision'])
            text=await self.writer.compose(thread=thread,history=history,decision=decision,knowledge=knowledge,stage=None)
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','В базе знаний не найден надёжный ответ. Нужна помощь менеджера.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
        revision=self.store.snapshot(thread_id)[0]['revision']
        return self.store.queue_action(thread_id,action_key=key,kind='reply',text=text,
                                       expected_revision=revision,due_at=int(self.clock()))

    async def on_silence(self, thread_id):
        thread,history=self._snapshot(thread_id)
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
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','Не удалось подготовить напоминание на основе базы знаний.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        return self.store.queue_action(thread_id,action_key=key,kind='reminder',text=text,
                                       expected_revision=thread['revision'],due_at=int(self.clock()))

    async def on_return(self,thread_id):
        thread,history=self._snapshot(thread_id)
        when=thread['next_contact_at']
        if thread['state']!='later' or when is None or when>self.clock():return None
        key='return:'+str(when)
        if self.store.action_for_key(thread_id,key):return None
        try:
            knowledge=await self.knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                question='Какие подтверждённые возможности по продукту полезны при возобновлении обсуждения?',request_id=uuid.uuid4().hex)
            self._check(thread_id,thread['revision'])
            text=await self.writer.compose(thread=thread,history=history,decision=None,knowledge=knowledge,stage='agreed_return')
        except KnowledgeUnavailable:
            self._check(thread_id,thread['revision'])
            self._handoff(thread,'knowledge_missing','Не удалось подготовить согласованное возвращение к клиенту.','knowledge_missing:'+key)
            return None
        self._check(thread_id,thread['revision'])
        self.store.set_state(thread_id,'engaged',expected_revision=thread['revision'])
        revision=self.store.snapshot(thread_id)[0]['revision']
        return self.store.queue_action(thread_id,action_key=key,kind='reply',text=text,expected_revision=revision,due_at=int(self.clock()))
