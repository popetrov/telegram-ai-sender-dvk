"""Owner-only knowledge configuration. Never distribute this configuration."""
import json
import os
from pathlib import Path
import uuid
from contextlib import nullcontext


def configure(state, source_env, company_id='dvk', *, center_locked=False):
    from dotenv import dotenv_values
    from file_lock import FileLock
    state=Path(state);path=state/'knowledge.json'
    values=dotenv_values(source_env)
    key,store=values.get('OPENAI_API_KEY'),values.get('VECTOR_STORE_ID')
    if not key or not store:
        raise ValueError('В настройках базы знаний не найдены ключ API и ID хранилища.')
    with (nullcontext() if center_locked else FileLock(state/'center.lock')):
        if path.exists():
            raise ValueError('База знаний уже настроена. Существующие настройки не заменены.')
        value=dict(format=1,bindings=[dict(company_id=company_id,business_id='dvk_finance',
                   products=['БГ','Кредит'],api_key=key,vector_store_id=store,model='gpt-4.1')])
        temporary=path.with_name('.knowledge-'+uuid.uuid4().hex+'.tmp')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False)
        if os.name!='nt':temporary.chmod(0o600)
        os.replace(temporary,path)
    print('База знаний подключена к центру локально. Ключ менеджерам не передаётся.')


def load_broker(state,authority,signing_key,encryption_key):
    path=Path(state)/'knowledge.json'
    if not path.exists():return None,[]
    if path.is_symlink():raise ValueError('Unsafe knowledge configuration')
    value=json.loads(path.read_text('utf-8'))
    if value.get('format')!=1 or not isinstance(value.get('bindings'),list):
        raise ValueError('Invalid knowledge configuration')
    from openai import AsyncOpenAI
    from dialogue_knowledge import VectorKnowledge
    from dialogue_knowledge_protocol import KnowledgeBroker
    providers={};clients=[]
    for row in value['bindings']:
        binding=(row['company_id'],row['business_id'])
        if binding in providers:raise ValueError('Duplicate knowledge binding')
        client=AsyncOpenAI(api_key=row['api_key'],timeout=60,max_retries=0)
        clients.append(client)
        providers[binding]=VectorKnowledge(client,business_id=row['business_id'],products=row['products'],
                                           vector_store_id=row['vector_store_id'],model=row.get('model','gpt-4.1'))
    return KnowledgeBroker(authority,providers,signing_key,encryption_key),clients
