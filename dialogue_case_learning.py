from finance_scope import account_allowed, thread_allowed
"""Per-dialogue case retrieval memory, using the existing knowledge provider."""
import json
from dialogue_seller import OWNER_ACCOUNT_ID

TOPICS=('existing_bank','insufficient_limit','decline','deadline','service_value','other')
PLAN_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'move':{'type':'string','enum':['diagnose','objection','value','next_step','answer']},
    'obstacle':{'type':'string'},'obstacle_confirmed':{'type':'boolean'},
    'case_topic':{'type':['string','null'],'enum':[*TOPICS,None]},
    'case_question':{'type':'string'},'next_step':{'type':'string'}},
    'required':['move','obstacle','obstacle_confirmed','case_topic','case_question','next_step']}


def validate_plan(plan):
    if not isinstance(plan,dict) or set(plan)!=set(PLAN_SCHEMA['required']):raise ValueError('Invalid sales plan')
    if plan['move'] not in PLAN_SCHEMA['properties']['move']['enum'] or type(plan['obstacle_confirmed']) is not bool:
        raise ValueError('Invalid sales move')
    if plan['case_topic'] not in (*TOPICS,None):raise ValueError('Invalid case topic')
    if any(not isinstance(plan[k],str) or len(plan[k])>1500 for k in ('obstacle','case_question','next_step')):
        raise ValueError('Invalid sales plan text')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS seller_case_cache (
        thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),product TEXT NOT NULL,
        topic TEXT NOT NULL,insight TEXT NOT NULL,sources TEXT NOT NULL,
        expires_at INTEGER NOT NULL,PRIMARY KEY(thread_id,product,topic))''')


async def retrieve(store,thread,plan,policy,knowledge,check_current):
    """No new knowledge bot. Reuse its cited result; historical numbers stay internal."""
    validate_plan(plan)
    topic=plan['case_topic']
    if not topic or not policy.case_request or not plan['case_question'].strip():return []
    if not thread['seller_enabled'] or not thread_allowed(thread):
        raise PermissionError('Case learning is owner-pilot only')
    if thread['mode']=='off':raise PermissionError('Case learning is disabled for this contact')
    check_current()
    identity=(thread['id'],thread['product'],topic)
    with store.connection() as db:
        row=db.execute('SELECT * FROM seller_case_cache WHERE thread_id=? AND product=? AND topic=?',identity).fetchone()
    if row and row['expires_at']>store.clock():
        return [dict(topic=topic,insight=row['insight'],sources=json.loads(row['sources']),historical_only=True)]
    import uuid
    import asyncio
    from dialogue_knowledge import KnowledgeUnavailable
    try:
        async with asyncio.timeout(12):
            answer=await knowledge.lookup(business_id=thread['business_id'],product=thread['product'],
                question=policy.case_request+'\nСитуация для поиска: '+plan['case_question'],request_id=uuid.uuid4().hex)
    except (KnowledgeUnavailable,TimeoutError):
        return []  # Case examples are supplementary; never invent missing experience.
    check_current()
    sources=[dict(id=e.id,file_id=e.file_id,filename=e.filename,text=e.text)
        for e in answer.evidence if 'кейс' in e.filename.casefold() and len(e.text)<=20000][:3]
    if not sources:return []
    with store.connection() as db:
        if store._thread(db,thread['id'])['revision']!=thread['revision']:
            from dialogue_store import StaleDialogue
            raise StaleDialogue('Conversation changed during case retrieval')
        db.execute('INSERT OR REPLACE INTO seller_case_cache VALUES (?,?,?,?,?,?)',
            (*identity,answer.answer,json.dumps(sources,ensure_ascii=False),int(store.clock())+86400))
    return [dict(topic=topic,insight=answer.answer,sources=sources,historical_only=True)]
