"""Automatically send the center's assigned queue through the protected sender."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

from access_lease import AccessDenied


log = logging.getLogger(__name__)
ZONE = timezone(timedelta(hours=4))
STATE = Path('data/bitrix-followup-auto-day')


def pause_snapshot(bot, exc):
    if isinstance(exc, bot.TelegramSpamPause):
        return "spam_pause", 0
    try:
        value = json.loads((bot.DATA_DIR / "send_cooldown.json").read_text("utf-8"))["until"]
        if isinstance(value, bool):
            raise ValueError("Invalid cooldown timestamp")
        until = float(value)
        if not math.isfinite(until) or not 0 < until <= 253402300799:
            raise ValueError("Invalid cooldown timestamp")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return "cooldown_unreadable", 0
    if until <= time.time():
        return "cooldown_recheck", 0
    return "cooldown_active", math.ceil(until)


class WarmupStatus:
    """Versioned metadata using the existing signed monitor stream."""
    def __init__(self, client):
        self.client = client
        self.last = None
        self.last_at = -float("inf")

    def observe(self, stage, *, assigned=None, queued=None, blocker="none", until=0):
        state = (stage, assigned, queued, blocker, until)
        now = time.monotonic()
        if state == self.last and now - self.last_at < 60:
            return
        assigned_code = {None: "u", True: "y", False: "n"}[assigned]
        queue_code = "u" if queued is None else str(queued)
        try:
            from incident_monitor import record, CODE_ROOT
            version = (CODE_ROOT / "version.txt").read_text("utf-8-sig").strip()
            record("monitor_warning", source="finance_warmup_status", stage=stage,
                   operation=f"v1_a{assigned_code}_q{queue_code}", reason_code=blocker,
                   count=until, connected=bool(self.client.is_connected()), version=version)
        except Exception as exc:
            log.debug("Warm-up status recording failed (%s)", type(exc).__name__)
        else:
            self.last = state
            self.last_at = now


async def run(application, client):
    from access_runtime import wait_initial_access, account_id
    import bot
    import bitrix_followup_sender as sender
    from unified_install import atomic

    monitor = WarmupStatus(client)
    monitor.observe('starting')
    await wait_initial_access()
    context = SimpleNamespace(application=application)
    checked_day = None
    unavailable = set()
    while True:
        today = datetime.now(ZONE).date().isoformat()
        if today != checked_day:
            unavailable.clear()
            checked_day = today
        assigned = None
        queued = None
        stage = 'access_check'
        try:
            assigned = sender.authorized().get('bitrix_followup_auto') is True
            if not assigned:
                monitor.observe('idle', assigned=False, blocker='not_assigned')
            elif bot.queue_is_active(context):
                monitor.observe('idle', assigned=True, blocker='another_queue_active')
            else:
                data = application.bot_data
                prior = data.get(bot.QUEUE_KEY)
                slot = {'state': 'sending', 'source': 'bitrix_followup_auto'}
                data[bot.QUEUE_KEY] = slot
                try:
                    stage = 'queue_check'
                    monitor.observe(stage, assigned=True)
                    result = await sender.center(client, 'list_auto')
                    if result.get('ok'):
                        queued = len(result['jobs'])
                        sent = 0
                        for index, job in enumerate(result['jobs']):
                            if job['id'] in unavailable:
                                continue
                            stage = 'dispatch'
                            monitor.observe(stage, assigned=True, queued=queued)
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
                        monitor.observe('cycle_finished', assigned=True, queued=queued)
                        atomic(STATE, (today+'\n').encode('ascii'))
                        if sent:
                            await application.bot.send_message(
                                account_id, f'Битрикс: отправлено {sent} из {len(result["jobs"])}.')
                    else:
                        monitor.observe('idle', assigned=True, blocker='queue_unavailable')
                finally:
                    if data.get(bot.QUEUE_KEY) is slot:
                        if prior is None:
                            data.pop(bot.QUEUE_KEY, None)
                        else:
                            data[bot.QUEUE_KEY] = prior
        except AccessDenied:
            monitor.observe(stage, assigned=assigned, queued=queued, blocker='access_denied')
        except bot.TelegramSendPause as exc:
            blocker, until = pause_snapshot(bot, exc)
            monitor.observe(stage, assigned=assigned, queued=queued, blocker=blocker, until=until)
        except Exception as exc:
            sender.pause_if_telegram_requires(exc)
            blocker = type(exc).__name__
            if blocker in {'FloodWaitError', 'PeerFloodError'}:
                try:
                    bot.require_send_cooldown_finished()
                except bot.TelegramSendPause as pause:
                    blocker, until = pause_snapshot(bot, pause)
                else:
                    until = 0
            else:
                until = 0
            monitor.observe(stage, assigned=assigned, queued=queued, blocker=blocker, until=until)
            log.warning('Daily Bitrix sender stopped for this check (%s)', type(exc).__name__)
        await asyncio.sleep(300)
