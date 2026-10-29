"""No Telegram deletion requests may leave an application-owned client."""
import asyncio
import base64
from contextvars import ContextVar
import json
from telethon import TelegramClient, utils
from telegram_safety import SafetyStop, console_notice, default_journal, validate_report

_report_permission = ContextVar('dvk_safety_report_permission', default=None)


class TelegramDeletionBlocked(SafetyStop):
    pass


def requests_in(request, seen=None):
    seen = set() if seen is None else seen
    if id(request) in seen:
        return
    seen.add(id(request))
    if utils.is_list_like(request):
        for item in request:
            yield from requests_in(item, seen)
    else:
        yield request
        nested = getattr(request, 'query', None)
        if nested is not None:
            yield from requests_in(nested, seen)


def ephemeral_media(request):
    """Inspect only TL media containers, never serialize texts or credentials."""
    seen=set()
    def visit(item):
        if item is None or id(item) in seen:return False
        seen.add(id(item))
        if utils.is_list_like(item):return any(visit(x) for x in item)
        if any(type(getattr(item,k,None)) is int and getattr(item,k)>0
               for k in ('ttl_seconds','ttl_period')):return True
        return any(visit(getattr(item,k,None)) for k in ('query','media','multi_media','extended_media'))
    return visit(request)


def safe_targets(request):
    """IDs only. Request repr/to_dict can expose customer text and auth keys."""
    fields={}; mids=[]
    for item in requests_in(request):
        peer=getattr(getattr(item,'peer',None),'user_id',None)
        if type(peer) is int and peer>0:fields['peer_id']=peer
        ids=getattr(item,'id',None)
        if utils.is_list_like(ids):
            for mid in ids:
                mid=getattr(mid,'id',mid)
                if type(mid) is int and mid>0:mids.append(mid)
    if mids:fields['message_ids']=list(dict.fromkeys(mids))[:2000]
    return fields


def forbidden_operation(request):
    for item in requests_in(request):
        name = type(item).__name__
        if name.startswith(('Delete', 'Destroy', 'Discard', 'Clear')) or name in (
                'LeaveChannelRequest', 'SetHistoryTTLRequest', 'SetDefaultHistoryTTLRequest',
                'UpdateDialogFilterRequest', 'UpdateDialogFiltersOrderRequest', 'EditPeerFoldersRequest'):
            return name


def require_non_destructive(request):
    if forbidden_operation(request) or ephemeral_media(request):
        raise TelegramDeletionBlocked('Удаление сообщений, истории и изменение папок программой запрещено.')


class ProtectedTelegramClient(TelegramClient):
    def __init__(self, *args, safety_journal=None, **kwargs):
        self.safety_journal = safety_journal if safety_journal is not None else default_journal()
        self._safety_failed = False
        super().__init__(*args, **kwargs)
        self._guard_sender(self._sender)

    def _guard_sender(self, sender):
        # Check the actual dispatch as well, including SDK retries after an await.
        if getattr(sender, '_dvk_safety_guard', False):
            return sender
        original = sender.send
        def guarded(request, *args, **kwargs):
            if utils.is_list_like(request):
                request = list(request)
            self._check_request(request)
            return original(request, *args, **kwargs)
        sender.send = guarded
        sender._dvk_safety_guard = True
        return sender

    async def _borrow_exported_sender(self, dc_id):
        return self._guard_sender(await super()._borrow_exported_sender(dc_id))

    def _deny(self, operation, request=None):
        # Block even if disk persistence fails. Only safe target IDs, never text/keys.
        self._safety_failed = True
        try:
            from incident_monitor import record
            import traceback
            from pathlib import Path
            stack=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
                   for f in traceback.extract_stack(limit=16)]
            record('request_blocked', operation=operation, call_stack=stack,
                   **(safe_targets(request) if request is not None else {}))
        except Exception:
            pass
        try:
            code = self.safety_journal.block(operation)['incident_id']
        except Exception:
            code = 'journal_unavailable'
        console_notice('Защита переписки: опасный запрос заблокирован до Telegram. Код: ' + code,
                       'Telegram safety: request blocked. Incident: ' + code)
        raise TelegramDeletionBlocked('Защита заблокировала изменение истории Telegram. Код: ' + code)

    def _check_request(self, request):
        operation = forbidden_operation(request)
        if operation:
            self._deny(operation,request)
        if ephemeral_media(request):
            self._deny('ephemeral_media_ttl',request)
        for item in requests_in(request):
            name = type(item).__name__
            if name.startswith(('Send', 'Forward', 'Edit')) or name == 'StartBotRequest':
                allowed = _report_permission.get()
                peer = getattr(getattr(item, 'peer', None), 'user_id', None)
                if (name in ('SendMessageRequest','SendMediaRequest') and allowed is not None
                        and peer == allowed[0] and getattr(item, 'message', None) == allowed[1]):
                    continue
                if self._safety_failed:
                    raise SafetyStop('Защита переписки остановила отправку. Нужна проверка владельца.')
                self.safety_journal.require_clear()
                if peer is not None:
                    from incident_isolation import require_peer_allowed,PeerQuarantined
                    try:require_peer_allowed(peer)
                    except PeerQuarantined:raise
                    except Exception:self._deny('quarantine_state_unreadable')

    async def __call__(self, request, *args, **kwargs):
        if utils.is_list_like(request):
            request = list(request)
        self._check_request(request)
        return await super().__call__(request, *args, **kwargs)

    async def _call(self, sender, request, *args, **kwargs):
        # Cover SDK internal dispatch too, before peer resolution/network calls.
        if utils.is_list_like(request):
            request = list(request)
        self._check_request(request)
        tracked=[]
        from incident_monitor import record
        for item in requests_in(request):
            if type(item).__name__=='SendMessageRequest' and _report_permission.get() is None:
                # These are our authenticated technical protocols, not client history.
                if (getattr(item,'message','') or '').startswith(('/lease ','/app_profile ','/research_status ')):
                    continue
                peer=getattr(getattr(item,'peer',None),'user_id',None)
                cached=self._mb_entity_cache.get(peer) if peer else None
                is_bot=bool(cached is not None and int(cached.ty)==ord('B'))
                if peer and not is_bot and peer != getattr(self,'_monitor_service_id',None):
                    rid=record('send_attempt',peer_id=peer,operation=type(item).__name__)
                    tracked.append((rid,peer))
        try:
            result=await super()._call(sender, request, *args, **kwargs)
        except BaseException as exc:
            for rid,peer in tracked:
                record('send_error',request_id=rid,peer_id=peer,error_type=type(exc).__name__)
            raise
        for rid,peer in tracked:
            ids=[]
            for response in result if isinstance(result,list) else [result]:
                if type(response).__name__=='UpdateShortSentMessage':
                    ids.append(response.id)
                for update in getattr(response,'updates',[]):
                    message=getattr(update,'message',None)
                    if message is not None and getattr(message,'out',False):
                        ids.append(message.id)
            record('send_result',request_id=rid,peer_id=peer,message_ids=ids)
            from incident_monitor import _default
            _default.observe(ids,peer)
        return result

    async def delete_messages(self, *args, **kwargs):
        self._deny('delete_messages')

    async def delete_dialog(self, *args, **kwargs):
        self._deny('delete_dialog')

    async def report_safety(self, authority, report, account_id, public_key):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from access_lease import canonical
        from access_runtime import transport_lock
        validate_report(report)
        text = '/safety ' + base64.urlsafe_b64encode(canonical(report)).decode()
        async with asyncio.timeout(45):
            entity = await self.get_entity(authority)
            if (not getattr(entity, 'bot', False)
                    or (entity.username or '').casefold() != authority.strip().lstrip('@').casefold()):
                raise ValueError('Safety receiver must be the configured service bot')
            async with transport_lock:
                async with self.conversation(entity, timeout=30, exclusive=True) as conversation:
                    token = _report_permission.set((entity.id, text))
                    try:
                        sent = await conversation.send_message(text, silent=True)
                    finally:
                        _report_permission.reset(token)
                    async with asyncio.timeout(30):
                        while True:
                            response = await conversation.get_response(sent)
                            if not (response.raw_text or '').startswith('DVK_SAFETY_ACK '):
                                continue
                            envelope = json.loads(response.raw_text[len('DVK_SAFETY_ACK '):])
                            payload = envelope['payload']
                            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
                                base64.b64decode(envelope['signature'], validate=True), canonical(payload))
                            if payload != dict(protocol='DVK_SAFETY_ACK_V1', incident_id=report['incident_id'],
                                               account_id=account_id, manager_id=report['manager_id']):
                                raise ValueError('Safety receipt identity mismatch')
                            return
