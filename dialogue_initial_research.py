from finance_scope import account_allowed, thread_allowed
"""Read only the research linked to this exact sent first message and owner binding."""
import hashlib
from dialogue_knowledge import KnowledgeUnavailable

def load(store,thread):
    if not thread_allowed(thread) or thread['company_id']!='dvk' or thread['business_id']!='dvk_finance':
        return []
    with store.connection() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'clients','messages','research_runs'}<=tables:return []
        rows=db.execute("""SELECT m.id message_id,r.id research_run_id,r.created_at,r.product,
                r.raw_result,c.inn,c.full_name
            FROM messages m JOIN clients c ON c.id=m.client_id
            JOIN research_runs r ON r.id=m.research_run_id AND r.client_id=c.id AND r.product=c.product
            WHERE c.manager_id=? AND c.telegram_id=? AND m.telegram_message_id=? AND m.status='sent'
            AND c.product IN ('БГ','Кредит') ORDER BY m.id DESC""",
            (thread['manager_id'],thread['peer_id'],thread['first_message_id'])).fetchall()
    if not rows:return []
    # Ambiguous identity/research is not a license to combine company dossiers.
    if len({(r['inn'],r['research_run_id']) for r in rows})!=1:
        raise KnowledgeUnavailable('Ambiguous initial company research')
    r=dict(rows[0]);text=r.pop('raw_result')
    if not isinstance(text,str) or len(text)>30000:
        raise KnowledgeUnavailable('Initial research requires manual review')
    return [{**r,'text':text,'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
             'source_kind':'historical_initial_research','current_company_situation_verified':False}]

