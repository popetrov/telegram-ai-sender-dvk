"""Verified snapshots of owner-designated KB methodology, not another knowledge base."""
import hashlib
import json
from pathlib import Path
import re

ROLES={'sales','credit_questionnaire','bg_questionnaire'}


def compile_source(source):
    text=source['text']
    if source['role']=='sales':
        position=text.find('ОСНОВЫ ПРОДАЖ')
        if position<0:raise ValueError('Sales method section missing')
        text=text[position:]  # Author biography/burnout are not the sender's persona.
    elif source['role']=='credit_questionnaire':
        # The parsed two-column form contains actual example customer data in
        # the right column. Only the question column is sent to the model.
        header=next((line for line in text.splitlines() if 'Наименование' in line and 'Источник' in line),None)
        match=re.search(r'^(.+?)\s{2,}\S',header or '')
        if not match:raise ValueError('Questionnaire columns not recognized')
        boundary=match.end()-1
        lines=[]
        for line in text.splitlines():
            if 'III БЛОК' in line:break
            if line.startswith('<PARSED'):continue
            question=line[:boundary].strip()
            if question:lines.append(question)
        text='\n'.join(lines)
        if any(token in text for token in ('ДИАТРАНС','1650364952','info@','Нурмухаметов')):
            raise ValueError('Example customer data remains in questionnaire')
        text='Структура вопросов; все заполненные ответы примера удалены. I блок — при запросе документов, II — при получении документов. Не задавать всё анкетой в начале.\n'+text
    if not text.strip() or len(text)>60000:raise ValueError('Invalid compiled method size')
    return dict(role=source['role'],file_id=source['file_id'],filename=source['filename'],
                source_sha256=source['sha256'],text=text)


def load(root,bindings,*,business_id):
    if not bindings:return []
    path=Path(root)/'data/sales-methodology.json'
    if path.is_symlink() or path.stat().st_size>512000:raise ValueError('Unsafe primary methodology cache')
    payload=json.loads(path.read_text('utf-8-sig'))
    if payload.get('format')!=1 or payload.get('business_id')!=business_id:raise ValueError('Methodology business mismatch')
    sources=payload.get('sources',[])
    if not isinstance(sources,list) or len(sources)!=len(bindings):raise ValueError('Incomplete primary methodology')
    result=[]
    for binding in bindings:
        matches=[s for s in sources if s.get('role')==binding['role'] and s.get('file_id')==binding['file_id']]
        if len(matches)!=1:raise ValueError('Missing or ambiguous primary method source')
        source=matches[0]
        digest=hashlib.sha256(source['text'].encode('utf-8')).hexdigest()
        if digest!=binding['sha256'] or source['sha256']!=digest:raise ValueError('Primary methodology version changed')
        result.append(compile_source(source))
    return result


def for_product(sources,product):
    wanted={'sales','credit_questionnaire' if product=='Кредит' else 'bg_questionnaire'}
    return [s for s in sources if s['role'] in wanted]


def ground_plan(decision,history,memory,sources,rules,product):
    """Business-configured gaps use cited customer facts, never inferred example values."""
    incoming={m['telegram_id']:m['text'] for m in history if m['direction']=='incoming'}
    accepted=[]
    for fact in decision.get('memory_facts',[]):
        if not isinstance(fact,dict):continue
        text=incoming.get(fact.get('source_message_id'),'')
        value,quote=fact.get('value'),fact.get('quote')
        if isinstance(value,str) and isinstance(quote,str) and value and quote and value in quote and quote in text:
            accepted.append(fact)
    if 'memory_facts' in decision:
        decision['memory_rejections']=len(decision['memory_facts'])-len(accepted)
        decision['memory_facts']=accepted
    known={f['field'] for f in [*memory,*accepted] if f.get('category')=='need'}
    normalize=lambda text:' '.join(text.casefold().split())
    customer=' '.join(normalize(text) for text in incoming.values())
    for rule in rules:
        if rule['product']!=product:continue
        document=next((s['text'] for s in sources if s['role']==rule['source_role']),'')
        if normalize(rule['source_anchor']) not in normalize(document):continue
        if (all(f in known for f in rule['trigger_fields']) and rule['missing_field'] not in known
                and any(normalize(p) in customer for p in rule['trigger_phrases'])):
            decision['required_question_focus']=rule['next_step']
            decision['needs_knowledge']=False
            decision['reply_required']=True
            if decision.get('sales_plan'):
                decision['sales_plan']={**decision['sales_plan'],'move':'diagnose','case_topic':None,
                                        'case_question':'','next_step':rule['next_step']}
            break
    return decision
