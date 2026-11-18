"""Start the assigned Finance seller after signed access and methodology arrive."""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from access_lease import AccessDenied
from business_updates import load_policy, verified_routes
from dialogue_knowledge_transport import RemoteKnowledge
from finance_scope import bind

log = logging.getLogger(__name__)
METHOD_REQUEST = '__DVK_PRIMARY_METHODS_V1__'


async def prepare_methods(client, root, policy, account, manager):
    from dialogue_methodology import load
    try:
        load(root, policy.primary_methodology, business_id=policy.business_id)
        return
    except (OSError, ValueError, KeyError):
        pass
    import access_runtime
    remote = RemoteKnowledge(
        client, authority=os.environ['ACCESS_BOT_USERNAME'].lstrip('@'),
        subject=f'{manager}:{account}',
        encryption_public_key=os.environ['SYNC_PUBLIC_KEY'],
        signing_public_key=Ed25519PublicKey.from_public_bytes(
            base64.b64decode(os.environ['ACCESS_PUBLIC_KEY'], validate=True)),
        transport_lock=access_runtime.transport_lock,
        require_access=access_runtime.require_access,
    )
    answer = await remote.lookup(business_id='dvk_finance', product='БГ',
                                 question=METHOD_REQUEST, request_id=uuid.uuid4().hex)
    import hashlib
    sources = []
    for binding in policy.primary_methodology:
        evidence = [e for e in answer.evidence if e.id == binding['role']
                    and e.file_id == binding['file_id']]
        if len(evidence) != 1:
            raise ValueError('Центр прислал неполный комплект методик.')
        item = evidence[0]
        digest = hashlib.sha256(item.text.encode('utf-8')).hexdigest()
        if digest != binding['sha256']:
            raise ValueError('Методики центра отличаются от назначенного пакета.')
        sources.append(dict(role=binding['role'], file_id=item.file_id,
                            filename=item.filename, text=item.text, sha256=digest))
    payload = dict(format=1, business_id='dvk_finance', sources=sources)
    from unified_install import json_write
    path = Path(root) / 'data/sales-methodology.json'
    if path.is_symlink():
        raise ValueError('Небезопасный файл методик.')
    json_write(path, payload)
    load(root, policy.primary_methodology, business_id=policy.business_id)


def settings_for(root, account):
    from unified_install import json_write
    path = Path(root) / 'data/dialogue-runtime.json'
    if path.exists():
        value = json.loads(path.read_text('utf-8-sig'))
        if value.get('owner_account_id') != account:
            raise PermissionError('Настройки диалогов принадлежат другому аккаунту.')
        return value
    now = int(time.time())
    value = dict(format=1, enabled=True, mode='owner_outbound',
                 business_id='dvk_finance', company_id='dvk', seller_stage1=True,
                 owner_account_id=account, after_message_id=0, activated_at=now,
                 watch_all_history=True, reply_only=True, history_watch_started_at=now,
                 attachments_enabled=True, attachments_started_at=now,
                 attachment_audio_model='gpt-4o-transcribe', stage3_calendar_enabled=True,
                 stage3_live_enabled=True, stage3_verified_base=True, finance_managed=True)
    json_write(path, value)
    return value


async def supervise(client, application, root, start_enabled):
    import access_runtime
    import config
    from dialogue_knowledge import KnowledgeUnavailable
    task = None
    last_error = None
    try:
        while True:
            try:
                if access_runtime._store is None or access_runtime.account_id is None:
                    raise AccessDenied('Ожидается проверка доступа центра.')
                grant = access_runtime._store.require_access()
                account = access_runtime.account_id
                routes = [r for r in verified_routes(access_runtime._store)
                          if r['company_id'] == 'dvk' and r['business_id'] == 'dvk_finance']
                legacy_owner = account == 925600480 and (Path(root) / 'data/dialogue-runtime.json').exists()
                if routes:
                    bind(account, config.MANAGER_ID, grant, routes[0])
                    policy = load_policy(root, access_runtime._store, 'dvk_finance',
                                         Path(root) / 'dialogue_profiles/dvk_finance.json')
                    if not policy.enabled:
                        return
                    await prepare_methods(client, root, policy, account, config.MANAGER_ID)
                    settings_for(root, account)
                elif not legacy_owner:
                    await asyncio.sleep(10)
                    continue
                task = await start_enabled(client, application, root)
                if task is not None:
                    await task
                return
            except (AccessDenied, KnowledgeUnavailable, OSError) as exc:
                error = type(exc).__name__
                if error != last_error:
                    log.warning('Автодиалоги ожидают разрешение или данные центра (%s)', error)
                    last_error = error
                await asyncio.sleep(10)
            except Exception as exc:
                log.exception('Автодиалоги остановлены при запуске')
                try:
                    import incident_monitor
                    incident_monitor.record('runtime_error', error_type=type(exc).__name__,
                                            stage='finance_activation')
                    await application.bot.send_message(
                        chat_id=access_runtime.account_id,
                        text='Автодиалоги Финанс не запущены. Требуется проверка руководителя. '
                             'Тип ошибки: ' + type(exc).__name__,
                    )
                except Exception:
                    log.warning('Уведомление о запуске автодиалогов ожидает связи')
                return
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
