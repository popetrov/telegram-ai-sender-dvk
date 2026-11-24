"""Collect actionable communication agreements; never execute calls or emails."""
import re

SCHEMA={'type':['object','null'],'additionalProperties':False,'properties':{
    'action':{'type':'string','enum':['call','write']},
    'recipient':{'type':['string','null']},
    'channel':{'type':['string','null'],'enum':['phone','email','telegram',None]},
    'address':{'type':['string','null']},'when':{'type':['string','null']},
    'purpose':{'type':['string','null']},'confirmed':{'type':'boolean'}},
    'required':['action','recipient','channel','address','when','purpose','confirmed']}


def route(decision,history,policy):
    """Incomplete or ungrounded details become questions, not a premature handoff."""
    if decision.get('intent')=='opt_out':return decision
    plan=decision.get('communication_plan')
    if plan is None and decision.get('intent')=='call':
        plan=dict(action='call',recipient='собеседник',channel=None,address=None,when=None,purpose=None,confirmed=False)
    if plan is None:return decision
    if (not isinstance(plan,dict) or set(plan)!=set(SCHEMA['required'])
            or plan['action'] not in {'call','write'} or plan['channel'] not in {'phone','email','telegram',None}
            or type(plan['confirmed']) is not bool
            or any(v is not None and (not isinstance(v,str) or not 0<len(v)<=700)
                   for k,v in plan.items() if k not in {'action','channel','confirmed'})):
        raise ValueError('Invalid communication plan')
    plan=dict(plan)
    incoming=[m['text'] for m in history if m['direction']=='incoming']
    customer='\n'.join(incoming)
    norm=lambda value:' '.join(value.casefold().split())
    latest=next((m['text'] for m in reversed(history) if m['direction']=='incoming'),'')
    preceding=next((m['text'] for m in reversed(history) if m['direction']=='outgoing'),'')
    agreed_previous=bool(re.fullmatch(r'\s*(?:да|верно|всё верно|все верно|правильно|договорились|подтверждаю)[.!\s]*',latest,re.I)
                         and re.search(r'верно|правильно|договорились|подтверд',preceding,re.I))
    if plan['address']:
        if plan['channel']=='phone':
            digits=lambda value:re.sub(r'\D','',value)
            numbers=re.findall(r'\+?\d[\d ()-]{6,}\d',customer)
            matched=next((p for p in numbers if digits(p)==digits(plan['address'])),None)
            plan['address']=matched
        elif norm(plan['address']) not in norm(customer):plan['address']=None
    if (plan['when'] and norm(plan['when']) not in norm(customer)
            and not (agreed_previous and norm(plan['when']) in norm(preceding))):plan['when']=None
    missing=None
    if not plan['recipient']:missing='Уточнить, с кем именно нужно связаться'
    elif not plan['channel']:missing='Уточнить канал связи: звонок в Telegram или по телефону, сообщение здесь или на почту'
    elif plan['channel']=='phone' and not plan['address']:missing='Попросить номер телефона нужного человека'
    elif plan['channel']=='email' and not plan['address']:missing='Попросить адрес электронной почты нужного человека'
    elif plan['channel']=='telegram' and plan['recipient']!='собеседник' and not plan['address']:missing='Попросить Telegram-контакт нужного человека'
    elif not plan['when']:missing='Уточнить, когда удобно связаться или когда нужно отправить информацию'
    elif (plan['action']=='call' and re.search(r'\d',plan['when'])
          and not re.search(r'мск|моск|utc|gmt|дуба|часов\w* пояс',plan['when'],re.I)):
        missing='Уточнить часовой пояс согласованного времени звонка'
    elif not plan['purpose']:missing='Уточнить, что именно нужно обсудить или отправить'
    directive=any(p.casefold() in latest.casefold() for p in (*policy.call_request_phrases,*policy.email_contact_phrases))
    accepted=agreed_previous
    if accepted:
        accepted=bool(plan['when'] and norm(plan['when']) in norm(preceding))
        if plan['address']:
            accepted=accepted and (re.sub(r'\D','',plan['address']) in re.sub(r'\D','',preceding)
                if plan['channel']=='phone' else norm(plan['address']) in norm(preceding))
    if not missing and not (plan['confirmed'] and (directive or accepted)):
        missing='Кратко подтвердить договорённость: кому, по какому контакту, когда и с какой целью; спросить, верно ли понял'
    decision={**decision,'communication_plan':plan,'application':None}
    if missing:
        decision.update(intent='question',reply_required=True,needs_knowledge=False,knowledge_question='',required_question_focus=missing)
        if decision.get('sales_plan'):
            decision['sales_plan']={**decision['sales_plan'],'move':'next_step','case_topic':None,'case_question':'','next_step':missing}
    else:
        decision['communication_complete']=plan
        decision['intent']='other'
    return decision


def summary(plan):
    return ('Согласовано действие для МОП (ещё НЕ выполнено):\n'
            +'Действие: '+('позвонить' if plan['action']=='call' else 'написать')
            +'\nКому: '+plan['recipient']+'\nКанал: '+plan['channel']
            +'\nКонтакт: '+(plan['address'] or 'текущий Telegram-диалог')
            +'\nКогда: '+plan['when']+'\nЦель: '+plan['purpose'])
