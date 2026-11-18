from finance_scope import account_allowed, thread_allowed
"""Owner pilot: durable, sourced facts; never an authority to send messages."""
from dialogue_store import StaleDialogue

FIELDS={
    'need':('amount','purpose','term','deadline','product','banks','security','contract',
            'guarantee_type','procurement_regime','validity','document_readiness',
            'offered_rate','target_rate','existing_solutions','prior_rejections','credit_portfolio',
            'consequences','guarantors','credit_history','decision_maker','document_contact',
            'current_need','need_timing','need_frequency'),
    'company':('name','inn','activity'),
    'relationship':('interest','important_date'),
    'style':('address','length','tone'),
    'conversation':('objection','agreement'),
    'restriction':('contact_preference',),
}
FACT_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'category':{'type':'string','enum':list(FIELDS)},
    'field':{'type':'string','enum':sorted({v for values in FIELDS.values() for v in values})},
    'value':{'type':'string'},'quote':{'type':'string'},'source_message_id':{'type':'integer'}},
    'required':['category','field','value','quote','source_message_id']}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS seller_memory_facts (
        thread_id TEXT NOT NULL REFERENCES dialogue_threads(id), category TEXT NOT NULL,
        field TEXT NOT NULL, value TEXT NOT NULL, quote TEXT NOT NULL,
        source_message_id INTEGER NOT NULL, source_text TEXT NOT NULL,
        observed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        PRIMARY KEY(thread_id,category,field,source_message_id),
        FOREIGN KEY(thread_id,source_message_id) REFERENCES dialogue_messages(thread_id,telegram_id))''')


def save(store,thread,history,facts,*,ttl_days=90):
    """Reject the entire batch on stale scope, unsourced claims or contradictions."""
    from dialogue_seller import OWNER_ACCOUNT_ID
    if not thread['seller_enabled'] or not thread_allowed(thread):
        raise PermissionError('Память второго этапа разрешена только владельцу.')
    if not isinstance(facts,list) or len(facts)>30:raise ValueError('Invalid memory facts')
    messages={m['telegram_id']:m for m in history}
    rows=[]
    seen=set()
    for fact in facts:
        if not isinstance(fact,dict) or set(fact)!=set(FACT_SCHEMA['required']):raise ValueError('Invalid memory fact')
        category,field=fact['category'],fact['field']
        if category not in FIELDS or field not in FIELDS[category]:raise ValueError('Invalid memory field')
        if type(fact['source_message_id']) is not int:raise ValueError('Invalid memory source')
        message=messages.get(fact['source_message_id'])
        if message is None or message['direction']!='incoming':raise ValueError('Memory requires customer evidence')
        value,quote=fact['value'],fact['quote']
        if (not isinstance(value,str) or not 0<len(value)<=500 or not isinstance(quote,str)
                or not 0<len(quote)<=2000 or quote not in message['text'] or value not in quote):
            raise ValueError('Memory fact is absent from source')
        identity=(category,field,message['telegram_id'])
        if identity in seen:raise ValueError('Conflicting memory facts')
        seen.add(identity)
        rows.append((thread['id'],category,field,value,quote,message['telegram_id'],message['text'],
                     message['sent_at'],int(message['sent_at'])+ttl_days*86400))
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=store._thread(db,thread['id'])
        if current['revision']!=thread['revision'] or current['mode']=='off':
            raise StaleDialogue('Диалог изменился до сохранения памяти.')
        for row in rows:
            actual=db.execute('SELECT text FROM dialogue_messages WHERE thread_id=? AND telegram_id=?',(row[0],row[5])).fetchone()
            if not actual or actual['text']!=row[6]:raise StaleDialogue('Исходная реплика изменилась.')
            previous=db.execute('SELECT value,quote,source_text FROM seller_memory_facts WHERE thread_id=? AND category=? AND field=? AND source_message_id=?',
                                (row[0],row[1],row[2],row[5])).fetchone()
            if previous and (previous['value'],previous['source_text'])!=(row[3],row[6]):raise ValueError('Conflicting memory replay')
            db.execute('INSERT OR IGNORE INTO seller_memory_facts VALUES (?,?,?,?,?,?,?,?,?)',row)


def snapshot(store,thread_id):
    """Return newest facts only; expired/edited evidence remains auditable, not active."""
    with store.connection() as db:
        rows=db.execute('''SELECT f.*,m.text AS current_source_text FROM seller_memory_facts f LEFT JOIN dialogue_messages m
            ON m.thread_id=f.thread_id AND m.telegram_id=f.source_message_id
            WHERE f.thread_id=? ORDER BY f.source_message_id DESC''',(thread_id,)).fetchall()
    latest={}
    for row in rows:latest.setdefault((row['category'],row['field']),dict(row))
    return [{k:v for k,v in row.items() if k!='current_source_text'} for row in latest.values()
            if row['expires_at']>store.clock() and row['source_text']==row['current_source_text']]
