"""Local usage and attachment metadata, without content uploads or bank operations."""
import uuid


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS seller_ai_usage (
        id TEXT PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),
        phase TEXT NOT NULL,model TEXT NOT NULL,response_id TEXT,status TEXT NOT NULL,
        input_tokens INTEGER,output_tokens INTEGER,created_at INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS seller_attachments (
        thread_id TEXT NOT NULL REFERENCES dialogue_threads(id),telegram_id INTEGER NOT NULL,
        kind TEXT NOT NULL,mime_type TEXT,size_bytes INTEGER,status TEXT NOT NULL,
        created_at INTEGER NOT NULL,PRIMARY KEY(thread_id,telegram_id))''')


def usage(store,thread_id,*,phase,model,result,status):
    def token(name):
        value=getattr(getattr(result,'usage',None),name,None)
        return value if type(value) is int and value>=0 else None
    usage_id=uuid.uuid4().hex
    with store.connection() as db:
        db.execute('INSERT INTO seller_ai_usage VALUES (?,?,?,?,?,?,?,?,?)',
            (usage_id,thread_id,phase,model,getattr(result,'id',None),status,
             token('input_tokens'),token('output_tokens'),int(store.clock())))

    try:
        from service_reports_runtime import record_usage
        record_usage(store.path.parent.parent,phase=phase,model=model,result=result,status=status,product=store.thread(thread_id)['product'],record_id=usage_id)
    except Exception:
        pass


def attachment(store,thread_id,message):
    """Inventory only. A file is never marked read just because it was received."""
    document=getattr(message,'document',None)
    kind=('voice' if getattr(message,'voice',None) else 'audio' if getattr(message,'audio',None)
          else 'photo' if getattr(message,'photo',None) else 'document' if document else 'other')
    size=getattr(document,'size',None)
    if type(size) is not int or size<0:size=None
    mime=getattr(document,'mime_type',None)
    if not isinstance(mime,str) or len(mime)>150:mime=None
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO seller_attachments VALUES (?,?,?,?,?,?,?)',
            (thread_id,message.id,kind,mime,size,'needs_manager_review',int(store.clock())))
