"""Approved encrypted bootstrap; existing installations retain their working pair on failure."""
import asyncio,base64,json,sqlite3,time,uuid
from contextlib import closing
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from app_profile_protocol import DOMAIN,PROTOCOL,open_response,validate_request
from access_lease import canonical
from sync_protocol import public_text
from telegram_app_migrate import env_values,private_atomic,read_profile,migrate,probe_application_pair,say
from telegram_app_trust import PROFILE_SIGNING_PUBLIC_KEY


def public_config(root):
    from transition_setup import validate_public
    result=json.loads((Path(root)/'center-public.json').read_text('utf-8'));validate_public(result)
    return result


def new_request(manager):
    key=X25519PrivateKey.generate()
    request=dict(protocol=PROTOCOL,request_id=uuid.uuid4().hex,nonce=uuid.uuid4().hex,
                 public_key=public_text(key),manager_id=manager)
    validate_request(request)
    return key,request


def cached_binding(root,public):
    path=Path(root)/'data/telegram-app-binding.private.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>16384:raise ValueError('Invalid cached binding')
    signed=json.loads(path.read_text('utf-8'));payload=signed['payload']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public['access_public_key'],validate=True)).verify(
        base64.b64decode(signed['signature'],validate=True),DOMAIN+canonical(payload))
    if payload['protocol']!='DVK_APP_RESPONSE_V1' or type(payload['account_id']) is not int or payload['account_id']<=0:
        raise ValueError('Invalid cached account')
    values=env_values((Path(root)/'.env').read_text('utf-8-sig'))
    if payload['manager_id']!=values.get('MANAGER_ID'):raise ValueError('Manager binding changed')
    return signed


def check_account(root,account):
    public=public_config(root);binding=cached_binding(root,public)
    if binding and binding['payload']['account_id']!=account:
        raise RuntimeError('Войдите в тот Telegram-аккаунт, которым подтвердили подключение к центру. Отправка остановлена.')


def accept(root,key,packet,request,public,account=None):
    signed=open_response(key,packet,request,public['access_public_key'],expected_account=account)
    profile=Path(root)/'data/telegram-app-profile.private.json'
    # Validate the independent owner signature before saving a binding or changing settings.
    candidate=Path(root)/'data/telegram-app-profile.incoming.private.json'
    private_atomic(candidate,canonical(signed['payload']['profile']))
    try:read_profile(candidate,PROFILE_SIGNING_PUBLIC_KEY)
    finally:candidate.unlink(missing_ok=True)
    private_atomic(profile,canonical(signed['payload']['profile']))
    private_atomic(Path(root)/'data/telegram-app-binding.private.json',canonical(signed))
    return profile


async def direct(root,values,request,public):
    """Copy only the auth key to memory. Never open a writable manager session."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
    name=values.get('SESSION_NAME','telegram_sender')
    path=(Path(root)/(name if name.endswith('.session') else name+'.session'))
    if not path.resolve().is_relative_to(Path(root).resolve()) or path.is_symlink():raise ValueError('Unsafe session')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT dc_id,server_address,port,auth_key FROM sessions').fetchone()
    if not row or not row[3]:raise ValueError('No authorized session')
    session=MemorySession();session.set_dc(*row[:3]);session.auth_key=AuthKey(row[3])
    client=TelegramClient(session,int(values['TELEGRAM_API_ID']),values['TELEGRAM_API_HASH'],
                          connection_retries=0,request_retries=0,flood_sleep_threshold=0)
    try:
        async with asyncio.timeout(45):
            await client.connect()
            if not await client.is_user_authorized():raise ValueError('Session not authorized')
            me=await client.get_me();bot=await client.get_entity(public['service_bot'])
            if not bot.bot:raise ValueError('Service is not a bot')
            async with client.conversation(bot,timeout=25,exclusive=True) as conversation:
                sent=await conversation.send_message('/app_profile '+base64.urlsafe_b64encode(canonical(request)).decode())
                reply=await conversation.get_response(sent)
                if not (reply.raw_text or '').startswith('DVK_APP '):raise ValueError('Profile not approved')
                packet=json.loads(reply.raw_text[len('DVK_APP '):])
                # Only this request and its response, never unrelated technical history.
                try:await client.delete_messages(bot,[sent.id,reply.id])
                except Exception:pass
                return packet,me.id
    finally:
        try:
            async with asyncio.timeout(8):await client.disconnect()
        except Exception:pass


def fresh(root,values,public,*,timeout=1200):
    from app_profile_broker import register,poll,validate_url
    import webbrowser
    url=validate_url(public.get('bootstrap_url'))
    key,request=new_request(values['MANAGER_ID'])
    register(url,request)
    link='https://t.me/'+public['service_bot']+'?start=app_'+request['request_id']
    say('Подтвердите подключение: откройте ссылку и нажмите «Запустить» в Telegram.\n'+link,
        'Confirm connection: open this link and press Start in Telegram.\n'+link)
    webbrowser.open(link)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        packet=poll(url,request['request_id'])
        if packet:return accept(root,key,packet,request,public)
        time.sleep(4)
    raise RuntimeError('Время ожидания истекло. Запустите программу ещё раз и подтвердите подключение.')


def prepare(root,*,sender_lock_held=False):
    root=Path(root).resolve();values=env_values((root/'.env').read_text('utf-8-sig'))
    existing=bool(values.get('TELEGRAM_API_ID') and values.get('TELEGRAM_API_HASH'))
    try:
        public=public_config(root)
        binding=cached_binding(root,public)
        if binding:
            profile=root/'data/telegram-app-profile.private.json'
            payload=read_profile(profile,PROFILE_SIGNING_PUBLIC_KEY)
            if binding['payload']['profile']['payload']!=payload:raise ValueError('Profile binding mismatch')
        elif existing:
            key,request=new_request(values['MANAGER_ID'])
            packet,account=asyncio.run(direct(root,values,request,public))
            profile=accept(root,key,packet,request,public,account)
        else:
            profile=fresh(root,values,public)
        payload=read_profile(profile,PROFILE_SIGNING_PUBLIC_KEY)
        pair=(payload['api_id'],payload['api_hash'])
        same=str(pair[0])==values.get('TELEGRAM_API_ID') and pair[1]==values.get('TELEGRAM_API_HASH')
        if not same:asyncio.run(probe_application_pair(pair))
        result=migrate(root,profile,PROFILE_SIGNING_PUBLIC_KEY,sender_lock_held=sender_lock_held)
        return result
    except Exception:
        if not existing:
            raise RuntimeError('Подключение не завершено. Убедитесь, что нажали «Запустить» у технического бота и руководитель выдал доступ. Затем повторите обычный запуск.') from None
        say('Автонастройка Telegram отложена. Запускаю с прежними рабочими настройками.',
            'Telegram setup deferred. Keeping the previous working settings.')
        return dict(status='deferred')
