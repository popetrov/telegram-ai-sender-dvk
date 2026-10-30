"""Enable exactly one known, previously contacted peer for a stopped pilot copy."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import uuid
from dotenv import dotenv_values
from file_lock import FileLock


def configure(root,*,username,product,company_id='dvk'):
    root=Path(root).resolve()
    if product not in {'БГ','Кредит'}:raise ValueError('Для пилота поддерживаются БГ и Кредит.')
    from startup_update import active_installation
    if active_installation(root)!=root:raise ValueError('Настраивайте активную рабочую папку, а не старую копию.')
    values=dotenv_values(root/'.env')
    manager=values.get('MANAGER_ID','').strip()
    username=username.strip().lstrip('@').casefold()
    if not manager or not username:raise ValueError('Не заданы менеджер или тестовый контакт.')
    with FileLock(root/'data/sender.lock'),FileLock(root/'data/startup.lock'):
        db=sqlite3.connect((root/'data/local.sqlite').as_uri()+'?mode=ro',uri=True)
        try:
            ids={r[0] for r in db.execute('''SELECT DISTINCT c.telegram_id FROM clients c JOIN messages m ON m.client_id=c.id
                WHERE c.manager_id=? AND lower(ltrim(c.username,'@'))=? AND c.product=? AND m.status='sent'
                AND m.telegram_message_id IS NOT NULL AND c.telegram_id IS NOT NULL''',(manager,username,product))}
        finally:db.close()
        if len(ids)!=1:raise ValueError('Нужно подтверждённое первое сообщение этому тестовому контакту по выбранному продукту. Рабочие клиенты не подключены.')
        value=dict(format=1,enabled=True,mode='pilot',business_id='dvk_finance',company_id=company_id,
                   peer_id=ids.pop(),product=product)
        path=root/'data/dialogue-runtime.json'
        if path.exists():
            if json.loads(path.read_text('utf-8'))==value:return value
            raise ValueError('Уже настроен другой пилот. Он не заменён автоматически.')
        temporary=path.with_name('.dialogue-runtime-'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        if os.name!='nt':temporary.chmod(0o600)
        os.replace(temporary,path)
    return value


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peer',required=True)
    parser.add_argument('--product',choices=['БГ','Кредит'],default='БГ')
    args=parser.parse_args()
    try:
        result=configure(__import__('installation_paths').state_root(),username=args.peer,product=args.product)
        print('Настроен только один тестовый контакт. Запустите программу привычным способом; сохранённая рассылка не запускается.')
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        print('Настройка не выполнена: '+str(exc))
        raise SystemExit(1)
