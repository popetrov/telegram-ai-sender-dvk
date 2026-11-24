"""Give the existing Bitrix queue priority over local calendar reminders."""
import asyncio
from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
_cache = {}
_refresh_lock = asyncio.Lock()
_external_sends = {}


def begin_external_send(peer):
    future = asyncio.get_running_loop().create_future()
    _external_sends[peer] = future
    return future


def finish_external_send(peer, future):
    if not future.done():
        future.set_result(None)
    if _external_sends.get(peer) is future:
        del _external_sends[peer]


async def wait_external_receipt(message):
    if not getattr(message, 'out', False):
        return
    future = _external_sends.get(getattr(message, 'chat_id', None))
    if future is not None:
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=90)
        except asyncio.TimeoutError:
            pass


def matches(worker, jobs):
    thread = worker.store.thread(worker.thread_id)
    peers = {job.get('peer') for job in jobs if type(job.get('peer')) is int}
    usernames = {str(job.get('username', '')).lstrip('@').casefold() for job in jobs}
    inns = {str(job.get('inn', '')).strip() for job in jobs}
    if thread['peer_id'] in peers:
        return True
    with worker.store.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='clients'").fetchone():
            return False
        rows = db.execute('SELECT inn,username FROM clients WHERE manager_id=? AND telegram_id=?',
                          (thread['manager_id'], thread['peer_id'])).fetchall()
    return any((str(r['inn'] or '').strip() in inns - {''}
                or str(r['username'] or '').lstrip('@').casefold() in usernames - {''})
               for r in rows)


async def calendar_allowed(worker):
    import access_runtime
    grant = access_runtime.require_access()
    if grant.get('bitrix_followup') is not True:
        return True
    subject = grant['subject']
    today = datetime.now(ZoneInfo('Europe/Saratov')).date().isoformat()
    async with _refresh_lock:
        cached = _cache.get(subject)
        if cached is None or cached['day'] != today or time.monotonic() - cached['at'] > 60:
            from bitrix_followup_sender import center
            try:
                result = await center(worker.client, 'calendar_priority')
                if result.get('ok') is not True or not isinstance(result.get('jobs'), list):
                    return False
                cached = dict(day=today, at=time.monotonic(), jobs=result['jobs'])
                _cache[subject] = cached
            except Exception as exc:
                log.warning('Локальный календарь ожидает сверку с Битриксом (%s)', type(exc).__name__)
                return False
    return not matches(worker, cached['jobs'])
