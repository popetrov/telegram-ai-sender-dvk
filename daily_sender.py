"""Automatically send the center's assigned queue through the protected sender."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import SimpleNamespace

from access_lease import AccessDenied


log = logging.getLogger(__name__)
ZONE = timezone(timedelta(hours=4))
STATE = Path('data/bitrix-followup-auto-day')


async def run(application, client):
    from access_runtime import wait_initial_access, account_id
    import bot
    import bitrix_followup_sender as sender
    from unified_install import atomic

    await wait_initial_access()
    context = SimpleNamespace(application=application)
    checked_day = None
    unavailable = set()
    while True:
        today = datetime.now(ZONE).date().isoformat()
        if today != checked_day:
            unavailable.clear()
            checked_day = today
        try:
            if (sender.authorized().get('bitrix_followup_auto') is True
                    and not bot.queue_is_active(context)):
                data = application.bot_data
                prior = data.get(bot.QUEUE_KEY)
                slot = {'state': 'sending', 'source': 'bitrix_followup_auto'}
                data[bot.QUEUE_KEY] = slot
                try:
                    result = await sender.center(client, 'list_auto')
                    if result.get('ok'):
                        sent = 0
                        for index, job in enumerate(result['jobs']):
                            if job['id'] in unavailable:
                                continue
                            if sender.authorized().get('bitrix_followup_auto') is not True:
                                raise AccessDenied('Daily send no longer assigned')
                            if bot.delivery_gate is not None:
                                status = await bot.delivery_gate.run(
                                    lambda: sender.dispatch(client, job, automatic=True), sender.guard)
                            else:
                                if index:
                                    await asyncio.sleep(bot.SEND_INTERVAL_SECONDS)
                                status = await sender.dispatch(client, job, automatic=True)
                            sent += status == 'отправлено'
                            if status == 'пропущено, username недоступен':
                                unavailable.add(job['id'])
                        atomic(STATE, (today+'\n').encode('ascii'))
                        if sent:
                            await application.bot.send_message(
                                account_id, f'Битрикс: отправлено {sent} из {len(result["jobs"])}.')
                finally:
                    if data.get(bot.QUEUE_KEY) is slot:
                        if prior is None:
                            data.pop(bot.QUEUE_KEY, None)
                        else:
                            data[bot.QUEUE_KEY] = prior
        except AccessDenied:
            pass
        except Exception as exc:
            sender.pause_if_telegram_requires(exc)
            log.warning('Daily Bitrix sender stopped for this check (%s)', type(exc).__name__)
        await asyncio.sleep(300)
