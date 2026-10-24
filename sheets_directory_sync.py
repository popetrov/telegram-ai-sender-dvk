"""Optional independent Google Sheets bridge; no Google credentials in manager packages."""
import asyncio
from getpass import getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.request
import uuid

log=logging.getLogger(__name__)


def validate_config(settings):
    if (settings.get('format')!=1 or not re.fullmatch(
            r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec',settings.get('url',''))
            or not re.fullmatch(r'[a-f0-9]{64}',settings.get('secret',''))):
        raise ValueError('Некорректная настройка подключения Google-таблицы')
    return settings


def configure(state):
    from file_lock import FileLock
    settings=dict(format=1,url=input('Адрес опубликованного скрипта Google: ').strip(),
                  secret=getpass('Секрет подключения (ввод скрыт): ').strip())
    validate_config(settings)
    path=Path(state)/'sheets.json'
    with FileLock(Path(state)/'center.lock'):
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            path.with_name('sheets.before-'+uuid.uuid4().hex+'.json').write_bytes(path.read_bytes())
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(settings),encoding='utf-8')
        if os.name!='nt': temporary.chmod(0o600)
        os.replace(temporary,path)
    print('Подключение сохранено локально. Секрет не пересылайте. Запустите центр.')


def signature(secret,message):
    return hmac.new(secret.encode(),message.encode('utf-8'),hashlib.sha256).hexdigest()


def send_profile(settings,profile,*,opener=urllib.request.urlopen):
    validate_config(settings)
    request_id=uuid.uuid4().hex
    message=json.dumps(dict(request_id=request_id,when=int(time.time()),
                           profile={k:profile[k] for k in ('account_id','full_name','number','department','company_name','revision')}),
                       ensure_ascii=False,separators=(',',':'))
    body=json.dumps(dict(message=message,signature=signature(settings['secret'],message)),ensure_ascii=False).encode('utf-8')
    request=urllib.request.Request(settings['url'],data=body,headers={'Content-Type':'application/json'},method='POST')
    with opener(request,timeout=25) as response:
        raw=response.read(32769)
    if len(raw)>32768: raise ValueError('Oversized Google response')
    envelope=json.loads(raw)
    signed=envelope['message']
    if not hmac.compare_digest(envelope['signature'],signature(settings['secret'],signed)):
        raise ValueError('Invalid Google bridge signature')
    result=json.loads(signed)
    if (result.get('request_id')!=request_id or result.get('account_id')!=profile['account_id']
            or result.get('revision')!=profile['revision'] or result.get('ok') is not True):
        raise ValueError('Google bridge did not confirm this profile')


async def loop(directory,state,notices=None):
    while True:
        try:
            settings=validate_config(json.loads((Path(state)/'sheets.json').read_text('utf-8')))
            for profile in directory.pending():
                await asyncio.to_thread(send_profile,settings,profile)
                directory.delivered(profile['account_id'],profile['revision'])
        except Exception as exc:
            log.warning('Google roster unavailable (%s); local profiles retained',type(exc).__name__)
            if notices is not None: notices.error('Google-таблица',exc)
        await asyncio.sleep(30)
