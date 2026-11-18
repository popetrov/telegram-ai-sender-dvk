"""Business-specific dialogue rules, without Telegram, AI or persistence."""
import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Handoff:
    notify: bool
    pause: bool
    label: str


class DialoguePolicy:
    def __init__(self, value):
        if not isinstance(value,dict) or value.get('schema_version') != 1:
            raise ValueError('Unsupported dialogue business package')
        self.business_id=value['business_id']
        self.products=tuple(value['products'])
        self.timezone=ZoneInfo(value['timezone'])
        self.enabled=value['enabled']
        self.mode=value['mode']
        self.model=value.get('model','gpt-4.1')
        self.prompts=value['prompts']
        self.contact_stop_phrases=tuple(value.get('contact_stop_phrases',()))
        self.ground_numbers=value.get('ground_numbers',False)
        self.role_ack=value.get('role_acknowledgement')
        self.need_ack=value.get('need_acknowledgement')
        self.reply_style=value.get('reply_style',False)
        self.seller_memory=value.get('seller_memory',False)
        self.seller_applications=value.get('seller_applications',False)
        self.case_learning=value.get('case_learning',False)
        self.complete_agreements=value.get('complete_agreements',False)
        if type(self.complete_agreements) is not bool:raise ValueError('Invalid agreement completion setting')
        self.primary_methodology=value.get('primary_methodology',[])
        self.methodology_focus_rules=value.get('methodology_focus_rules',[])
        rule_keys={'product','source_role','source_anchor','trigger_fields','missing_field','trigger_phrases','next_step'}
        if not isinstance(self.methodology_focus_rules,list) or len(self.methodology_focus_rules)>20:
            raise ValueError('Invalid methodology focus rules')
        for rule in self.methodology_focus_rules:
            if (not isinstance(rule,dict) or set(rule)!=rule_keys
                    or any(not isinstance(rule[k],str) or not 1<=len(rule[k])<=500 for k in rule_keys-{'trigger_fields','trigger_phrases'})
                    or any(not isinstance(rule[k],list) or not 0<=len(rule[k])<=20
                        or any(not isinstance(s,str) or not 1<=len(s)<=150 for s in rule[k]) for k in ('trigger_fields','trigger_phrases'))):
                raise ValueError('Invalid methodology focus rule')
        from dialogue_methodology import ROLES
        if (not isinstance(self.primary_methodology,list) or (self.primary_methodology and
                (len(self.primary_methodology)!=3 or {s.get('role') for s in self.primary_methodology if isinstance(s,dict)}!=ROLES))
                or any(not isinstance(s,dict) or set(s)!={'role','file_id','sha256'}
                    or not isinstance(s['file_id'],str) or not s['file_id']
                    or not isinstance(s['sha256'],str) or len(s['sha256'])!=64 for s in self.primary_methodology)):
            raise ValueError('Invalid primary methodology bindings')
        if type(self.case_learning) is not bool:raise ValueError('Invalid case learning setting')
        self.case_request=value.get('case_request','')
        self.conversation_basis=value.get('conversation_basis','')
        self.knowledge_required_phrases=tuple(value.get('knowledge_required_phrases',()))
        if not isinstance(self.conversation_basis,str) or len(self.conversation_basis)>2000:raise ValueError('Invalid conversation basis')
        if not isinstance(self.case_request,str) or len(self.case_request)>2000:raise ValueError('Invalid case request')
        self.declined_answers=tuple(value.get('declined_answers',()))
        self.restricted_knowledge_markers=tuple(value.get('restricted_knowledge_markers',()))
        self.exploration_phrases=tuple(value.get('exploration_phrases',()))
        self.document_questions=tuple(value.get('document_questions',()))
        self.public_document_sections=tuple(value.get('public_document_sections',()))
        self.service_objection_phrases=tuple(value.get('service_objection_phrases',()))
        self.service_objection_question=value.get('service_objection_question','')
        self.email_contact_phrases=tuple(value.get('email_contact_phrases',()))
        self.call_request_phrases=tuple(value.get('call_request_phrases',()))
        if not isinstance(self.service_objection_question,str) or len(self.service_objection_question)>2000:
            raise ValueError('Invalid service value knowledge request')
        if len(self.public_document_sections)>10 or any(not isinstance(s,dict) or set(s)!={'start','end'}
                or any(not isinstance(v,str) or not 3<=len(v)<=200 for v in s.values()) for s in self.public_document_sections):
            raise ValueError('Invalid public document sections')
        for phrases in (self.declined_answers,self.restricted_knowledge_markers,self.exploration_phrases,self.document_questions,self.service_objection_phrases,self.knowledge_required_phrases,self.email_contact_phrases,self.call_request_phrases):
            if len(phrases)>40 or any(not isinstance(p,str) or not 2<=len(p)<=200 for p in phrases):
                raise ValueError('Invalid refusal or restricted knowledge rules')
        if type(self.seller_applications) is not bool:raise ValueError('Invalid applications setting')
        if type(self.seller_memory) is not bool:raise ValueError('Invalid seller memory setting')
        if type(self.reply_style) is not bool:raise ValueError('Invalid reply style setting')
        for step in (self.role_ack,self.need_ack):
            if step is None:continue
            if (not isinstance(step,dict) or set(step)!={'answers','questions','reply'}
                    or any(not isinstance(step[k],list) or not 0<len(step[k])<=30
                           or any(not isinstance(s,str) or not 0<len(s)<=300 for s in step[k])
                           for k in ('answers','questions'))
                    or not isinstance(step['reply'],str) or not 0<len(step['reply'])<=500
                    or step['reply'].count('?')!=1):
                raise ValueError('Invalid conversational acknowledgement rule')
        if type(self.ground_numbers) is not bool:raise ValueError('Invalid numeric grounding setting')
        if (len(self.contact_stop_phrases)>50 or any(not isinstance(p,str) or not 3<=len(p)<=150 for p in self.contact_stop_phrases)):
            raise ValueError('Invalid contact-wide stop phrases')
        self.acknowledgements=value.get('event_acknowledgements',{})
        self.apology=value['opt_out_apology']
        self.steps=tuple(value['silent_reminders'])
        self.knowledge=value['knowledge']
        self.handoffs={k:Handoff(**v) for k,v in value['handoffs'].items()}
        if (not isinstance(self.business_id,str) or not self.business_id.strip()
                or type(self.enabled) is not bool or self.mode not in {'automatic','draft'}
                or not self.products or any(not isinstance(p,str) or not p.strip() for p in self.products)
                or len(set(self.products))!=len(self.products)
                or not isinstance(self.apology,str) or not self.apology.strip() or len(self.apology)>4096):
            raise ValueError('Invalid dialogue package settings')
        if (not isinstance(self.model,str) or not self.model.strip() or not isinstance(self.prompts,dict)
                or set(self.prompts)!={'analyst','writer'}
                or any(not isinstance(v,str) or not v.strip() or len(v)>16000 for v in self.prompts.values())):
            raise ValueError('Invalid business dialogue instructions')
        if (not isinstance(self.acknowledgements,dict) or set(self.acknowledgements)-{'call','documents'}
                or any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in self.acknowledgements.values())):
            raise ValueError('Invalid handoff acknowledgements')
        if self.knowledge.get('adapter') not in {'owner_vector_store'}:
            raise ValueError('Unknown knowledge adapter')
        if not isinstance(self.knowledge.get('bot'),str) or not self.knowledge['bot'].endswith('_bot'):
            raise ValueError('Invalid knowledge bot')
        for step in self.steps:
            if (not isinstance(step,dict) or set(step)!={'unit','count'}
                    or step['unit'] not in {'days','months'} or type(step['count']) is not int
                    or not 1<=step['count']<=365):
                raise ValueError('Invalid follow-up interval')
        if len(self.steps)>12:
            raise ValueError('Too many follow-up stages')
        required={'ready','need','call','documents','knowledge_missing'}
        if not required.issubset(self.handoffs):
            raise ValueError('Missing handoff rules')
        if any(type(v.notify) is not bool or type(v.pause) is not bool or not v.label for v in self.handoffs.values()):
            raise ValueError('Invalid handoff rules')

    @classmethod
    def load(cls, path):
        path=Path(path)
        if path.is_symlink() or path.stat().st_size>128*1024:
            raise ValueError('Unsafe dialogue package')
        return cls(json.loads(path.read_text('utf-8-sig')))

    def next_reminder_at(self, first_sent_at, sent_reminders, *, has_reply=False):
        """Each interval starts at the preceding *successful* send, not at generation.

        An overdue schedule emits one step; it cannot catch up by sending three
        reminders together after a long outage. Calendar months keep local time.
        """
        times=[first_sent_at,*sent_reminders]
        if (any(type(t) is not int or t<=0 for t in times)
                or any(b<=a for a,b in zip(times,times[1:])) or len(sent_reminders)>len(self.steps)):
            raise ValueError('Invalid confirmed follow-up history')
        if has_reply or len(sent_reminders)==len(self.steps):
            return None
        interval=self.steps[len(sent_reminders)]
        previous=datetime.fromtimestamp(times[-1],self.timezone)
        if interval['unit']=='days':
            result=previous+timedelta(days=interval['count'])
        else:
            month_index=previous.year*12+previous.month-1+interval['count']
            year,month=divmod(month_index,12);month+=1
            result=previous.replace(year=year,month=month,
                                    day=min(previous.day,calendar.monthrange(year,month)[1]))
        return int(result.timestamp())

    def handoff(self,event):
        if event not in self.handoffs:
            raise ValueError('Unknown handoff event')
        return self.handoffs[event]
