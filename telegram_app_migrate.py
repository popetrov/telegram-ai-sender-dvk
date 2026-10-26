"""Private app-credential rollout. Never includes or replaces user sessions.

Import-safe, no network or filesystem mutations until migrate() is called.
The public signing key is pinned by the build, never supplied by the profile.
"""
import argparse
import asyncio
import base64
from contextlib import contextmanager,closing,ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

DOMAIN=b'DVK_TELEGRAM_APP_PROFILE_V1\n'
API_KEYS=('TELEGRAM_API_ID','TELEGRAM_API_HASH')
FIELDS={'protocol','version','api_id','api_hash','app_title'}


class MigrationDeferred(RuntimeError):pass


def say(text,fallback):
    # A narrow Windows output encoding must never turn a safe deferral into a crash.
    try:print(text,flush=True)
    except UnicodeEncodeError:print(fallback,flush=True)


def canonical(payload):
    return json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')


def validate_pair(api_id,api_hash):
    if isinstance(api_id,str) and api_id.isdigit():api_id=int(api_id)
    if type(api_id) is not int or api_id<=0 or not isinstance(api_hash,str) or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):
        raise MigrationDeferred('Некорректные данные приложения; прежние настройки сохранены.')
    return api_id,api_hash


def read_profile(path,public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>8192:raise MigrationDeferred('Небезопасный файл профиля.')
    try:
        envelope=json.loads(path.read_text('utf-8'))
        if set(envelope)!={'payload','signature'}:raise ValueError()
        payload=envelope['payload']
        if set(payload)!=FIELDS or payload['protocol']!='DVK_TELEGRAM_APP_PROFILE_V1':raise ValueError()
        if type(payload['version']) is not int or payload['version']<=0:raise ValueError()
        if not isinstance(payload['app_title'],str) or not payload['app_title']:raise ValueError()
        validate_pair(payload['api_id'],payload['api_hash'])
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key,validate=True)).verify(
            base64.b64decode(envelope['signature'],validate=True),DOMAIN+canonical(payload))
        return payload
    except Exception:
        raise MigrationDeferred('Подпись профиля не подтверждена; прежние настройки сохранены.') from None


def env_values(text):
    # Parse without evaluating substitutions or importing the user's application.
    values={}
    for line in text.splitlines():
        match=re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$',line)
        if not match:continue
        key,value=match.groups()
        if key in values:raise MigrationDeferred('Повторяющиеся поля в .env; автоматическая замена отложена.')
        if value.startswith(('"',"'")):
            quote=value[0];end=value.find(quote,1)
            tail=value[end+1:].strip() if end>=0 else ''
            if end<0 or (tail and not tail.startswith('#')):
                raise MigrationDeferred('Сложный формат .env; автоматическая замена отложена.')
            value=value[1:end]
        else:value=value.split(' #',1)[0].strip()
        values[key]=value
    return values


def replace_pair(text,pair):
    newline='\r\n' if '\r\n' in text else '\n'
    lines=text.splitlines(keepends=True);seen=set();result=[]
    for line in lines:
        match=re.match(r'^(\s*(?:export\s+)?)(TELEGRAM_API_ID|TELEGRAM_API_HASH)\s*=.*?(\r?\n)?$',line)
        if not match:result.append(line);continue
        prefix,key,ending=match.groups();seen.add(key)
        result.append(prefix+key+'='+str(pair[API_KEYS.index(key)])+(ending or ''))
    for key,value in zip(API_KEYS,pair):
        if key not in seen:
            if result and not result[-1].endswith(('\n','\r')):result[-1]+=newline
            result.append(key+'='+str(value)+newline)
    return ''.join(result)


def private_atomic(path,data):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink():raise MigrationDeferred('Небезопасный путь настройки.')
    path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(handle,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def _one_lock(path):
    # Same OS-held sender lock as the application; leaves no fake lock state.
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if path.stat().st_size==0:stream.write(b'0');stream.flush();stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise MigrationDeferred('Программа уже запущена; настройки не заменены.') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


@contextmanager
def stopped_lock(root):
    with ExitStack() as stack:
        for name in ('startup.lock','sender.lock'):stack.enter_context(_one_lock(Path(root)/'data'/name))
        yield


async def probe_existing_session(root,values,old,new):
    """Two sequential read-only identity checks, with in-memory session copies.

    Never calls start/sign_in/send_code_request/qr_login, and never writes a
    manager's SQLite session. Any network/auth error leaves their .env untouched.
    """
    from safe_telegram import ProtectedTelegramClient as TelegramClient
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession
    name=values.get('SESSION_NAME','telegram_sender')
    if not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise MigrationDeferred('Небезопасное имя сессии.')
    path=Path(root)/(name if name.endswith('.session') else name+'.session')
    if not path.exists():raise MigrationDeferred('Сохранённой сессии ещё нет; требуется первый вход пользователя.')
    if path.is_symlink():raise MigrationDeferred('Небезопасный путь сессии.')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT dc_id,server_address,port,auth_key FROM sessions').fetchone()
    if row is None or not row[3]:raise MigrationDeferred('Сессия не авторизована.')
    async def identity(pair):
        session=MemorySession();session.set_dc(row[0],row[1],row[2]);session.auth_key=AuthKey(row[3])
        client=TelegramClient(session,*pair,receive_updates=False,connection_retries=0,request_retries=0)
        try:
            async with asyncio.timeout(40):
                await client.connect()
                if not await client.is_user_authorized():raise MigrationDeferred('Проверка авторизации не пройдена.')
                me=await client.get_me()
                if me is None or type(me.id) is not int:raise MigrationDeferred('Не подтверждён аккаунт менеджера.')
                return me.id
        finally:
            try:
                async with asyncio.timeout(10):await client.disconnect()
            except Exception:pass
    try:
        old_id=await identity(old);new_id=await identity(new)
        if old_id!=new_id:raise MigrationDeferred('Изменился аккаунт пользователя.')
        return dict(account_id=old_id,same_account=True,different_api_pair=old!=new)
    except MigrationDeferred:raise
    except Exception as exc:
        raise MigrationDeferred('Проверка соединения не пройдена: '+type(exc).__name__+'. Прежние настройки сохранены.') from None


async def probe_application_pair(pair):
    """Validate the pair on a fresh unauthenticated connection, without phone/SMS/login.

    A short-lived login token is neither shown nor accepted and is discarded.
    No user account is authorized and no persistent session is created.
    """
    from telethon import functions,types
    from safe_telegram import ProtectedTelegramClient as TelegramClient
    from telethon.sessions import MemorySession
    pair=validate_pair(*pair)
    client=TelegramClient(MemorySession(),*pair,receive_updates=False,connection_retries=0,request_retries=0)
    try:
        async with asyncio.timeout(35):
            await client.connect()
            result=await client(functions.auth.ExportLoginTokenRequest(api_id=pair[0],api_hash=pair[1],except_ids=[]))
            if not isinstance(result,types.auth.LoginToken) or not result.token:
                raise MigrationDeferred('Параметры приложения не подтверждены для нового входа.')
            return dict(application_pair_accepted=True,account_logged_in=False,login_codes_requested=0)
    except MigrationDeferred:raise
    except Exception as exc:
        raise MigrationDeferred('Проверка API-пары не пройдена: '+type(exc).__name__+'. Настройки не изменены.') from None
    finally:
        try:
            async with asyncio.timeout(10):await client.disconnect()
        except Exception:pass


def migrate(root,profile,public_key,*,verify=probe_existing_session,sender_lock_held=False):
    root=Path(root).resolve()
    if (root/'data').is_symlink():raise MigrationDeferred('Небезопасная папка данных.')
    payload=read_profile(profile,public_key);new=validate_pair(payload['api_id'],payload['api_hash'])
    def perform():
        env=root/'.env';marker=root/'data/telegram-app-migration.json'
        if env.is_symlink() or marker.is_symlink():raise MigrationDeferred('Небезопасный путь настройки.')
        had_env=env.exists();old_bytes=env.read_bytes() if had_env else b''
        text=old_bytes.decode('utf-8-sig');values=env_values(text)
        old=validate_pair(values.get(API_KEYS[0]),values.get(API_KEYS[1])) if any(values.get(k) for k in API_KEYS) else None
        prior=json.loads(marker.read_text('utf-8')) if marker.exists() else None
        if prior and payload['version']<prior['version']:raise MigrationDeferred('Понижение версии профиля запрещено.')
        if old==new:
            if prior is None:
                private_atomic(marker,json.dumps(dict(format=1,version=payload['version'],app_id=new[0],
                    created_at=int(time.time()),backup=None,expected_env_sha256=hashlib.sha256(old_bytes).hexdigest())).encode())
            elif payload['version']>prior['version']:
                prior['version']=payload['version'];private_atomic(marker,json.dumps(prior).encode())
            return dict(status='already_current',version=payload['version'])
        if prior and payload['version']==prior['version']:raise MigrationDeferred('Другие ключи с прежним номером версии запрещены.')
        session_name=values.get('SESSION_NAME','telegram_sender')
        session=root/(session_name if session_name.endswith('.session') else session_name+'.session')
        if not session_name or not session.resolve().is_relative_to(root) or session.is_symlink():
            raise MigrationDeferred('Небезопасный путь сессии.')
        if session.exists():
            if old is None:raise MigrationDeferred('Сессия есть, но прежние API-настройки не определены.')
            asyncio.run(verify(root,values,old,new))
        # A clean installation has no session to preserve; normal first login
        # will use the bundled app while every other setting remains unchanged.
        updated=(b'\xef\xbb\xbf' if old_bytes.startswith(b'\xef\xbb\xbf') else b'')+replace_pair(text,new).encode('utf-8')
        backup=root/'data/telegram-app-backups'/('env-'+uuid.uuid4().hex+'.private')
        if backup.parent.is_symlink():raise MigrationDeferred('Небезопасная папка резервных копий.')
        private_atomic(backup,old_bytes)
        info=dict(format=1,version=payload['version'],app_id=new[0],created_at=int(time.time()),
            backup=backup.relative_to(root).as_posix(),expected_env_sha256=hashlib.sha256(updated).hexdigest())
        try:
            private_atomic(env,updated)
            private_atomic(marker,json.dumps(info,sort_keys=True).encode())
        except BaseException:
            if had_env:private_atomic(env,old_bytes)
            else:env.unlink(missing_ok=True)
            raise
        return dict(status='migrated',version=payload['version'],verified_existing_session=session.exists())
    if sender_lock_held:return perform()
    with stopped_lock(root):return perform()


def rollback(root):
    root=Path(root).resolve()
    with stopped_lock(root):
        marker=root/'data/telegram-app-migration.json';info=json.loads(marker.read_text('utf-8'))
        if not info.get('backup'):raise MigrationDeferred('Данные уже совпадали; замены для отката не было.')
        backup=(root/info['backup']).resolve()
        if not backup.is_relative_to(root/'data/telegram-app-backups') or backup.is_symlink():raise MigrationDeferred('Небезопасный путь резервной копии.')
        old=env_values(backup.read_text('utf-8-sig'))
        pair=validate_pair(old.get(API_KEYS[0]),old.get(API_KEYS[1]))
        env=root/'.env';current=env.read_bytes().decode('utf-8-sig')
        if hashlib.sha256(env.read_bytes()).hexdigest()!=info['expected_env_sha256']:
            raise MigrationDeferred('Настройки изменились после перехода; автоматический откат отложен.')
        private_atomic(env,replace_pair(current,pair).encode('utf-8'))
        info['rolled_back']=True;private_atomic(marker,json.dumps(info).encode())
        return dict(status='rolled_back',version=info['version'])


def apply_bundled_profile(root,*,sender_lock_held=False,offline=False):
    root=Path(root);profile=root/'telegram-app-profile.private.json'
    if not profile.exists() or offline:return dict(status='not_requested')
    try:
        from telegram_app_trust import PROFILE_SIGNING_PUBLIC_KEY
        return migrate(root,profile,PROFILE_SIGNING_PUBLIC_KEY,sender_lock_held=sender_lock_held)
    except (MigrationDeferred,OSError,ValueError,ImportError):
        say('Настройки приложения Telegram сохранены прежними; переход отложен.',
            'Telegram application settings unchanged; migration deferred.')
        return dict(status='deferred')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path);parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(rollback(args.root) if args.rollback else apply_bundled_profile(args.root)))
    except Exception as exc:
        print('Переход не выполнен: '+type(exc).__name__+'. Секреты в журнал не включены.')
        raise SystemExit(1)
