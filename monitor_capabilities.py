"""Signed, bounded capability probe. It never changes access permissions."""
import asyncio,base64,json,os,re,time,uuid
from access_lease import canonical
_cached={}

def register(application,store,key):
    from telegram.ext import CommandHandler
    async def answer(update,context):
        if update.effective_chat.type!='private' or len(context.args)!=1 or not re.fullmatch('[0-9a-f]{32}',context.args[0]):return
        account=update.effective_user.id
        if not any(r['account_id']==account and r['company_id'] for r in store.list_managers()):return
        payload=dict(protocol='DVK_MONITOR_CAPS_V1',account_id=account,nonce=context.args[0],forensic_v1=True)
        envelope=dict(payload=payload,signature=base64.b64encode(key.sign(canonical(payload))).decode())
        await update.effective_message.reply_text('DVK_MONITOR_CAPS '+json.dumps(envelope,separators=(',',':')),disable_notification=True)
    application.add_handler(CommandHandler('monitor_caps',answer),group=-5)

async def supported(client):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from access_runtime import transport_lock
    from safe_telegram import _report_permission
    authority=os.environ['ACCESS_BOT_USERNAME'].lstrip('@');now=time.monotonic()
    previous=_cached.get(authority)
    if previous and previous[0]>now:return previous[1]
    ok=False
    try:
        async with asyncio.timeout(4):
            entity=await client.get_entity(authority);me=await client.get_me()
            if not entity.bot or (entity.username or '').casefold()!=authority.casefold():raise ValueError('Unexpected capability receiver')
            nonce=uuid.uuid4().hex;text='/monitor_caps '+nonce
            key=Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'],validate=True))
            async with transport_lock:
                async with client.conversation(entity,timeout=3,exclusive=True) as conversation:
                    token=_report_permission.set((entity.id,text))
                    try:sent=await conversation.send_message(text,silent=True)
                    finally:_report_permission.reset(token)
                    while True:
                        response=await conversation.get_response(sent)
                        raw=response.raw_text or ''
                        if not raw.startswith('DVK_MONITOR_CAPS '):continue
                        envelope=json.loads(raw[len('DVK_MONITOR_CAPS '):]);payload=envelope['payload']
                        key.verify(base64.b64decode(envelope['signature'],validate=True),canonical(payload))
                        ok=payload==dict(protocol='DVK_MONITOR_CAPS_V1',account_id=me.id,nonce=nonce,forensic_v1=True)
                        break
    except (Exception,):pass
    _cached[authority]=(time.monotonic()+300,ok)
    return ok
