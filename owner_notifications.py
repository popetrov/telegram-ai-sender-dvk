"""Durable, deduplicated owner notices; never business records or protocol traffic."""
import asyncio
from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def manager_label(store, account_id):
    """Owner-only identity from the internal card; never from manager input."""
    if store is not None:
        with store.connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_directory'").fetchone():
                row = db.execute('SELECT d.full_name FROM manager_directory d JOIN managers m '
                                 'ON m.account_id=d.account_id WHERE d.account_id=? AND m.company_id IS NOT NULL',
                                 (account_id,)).fetchone()
                if row and row['full_name'].strip():
                    return f"{row['full_name']} (ID {account_id})"
    return str(account_id)


class OwnerNotifications:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS owner_notices (
                notice_key TEXT PRIMARY KEY, text TEXT NOT NULL, created_at INTEGER NOT NULL,
                sent_at INTEGER)''')

    def once(self, key, text, now=None):
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO owner_notices VALUES (?,?,?,NULL)',
                       (key, text, int(time.time() if now is None else now)))

    def error(self, component, exception, account_id=None, now=None):
        now = int(time.time() if now is None else now)
        # At most one notice per component/account per hour, including restarts.
        key = f'error:{component}:{account_id}:{now//3600}'
        who = f' Менеджер: {manager_label(self.store, account_id)}.' if account_id is not None else ''
        self.once(key, f'Ошибка: {component}.{who} Требуется проверить консоль центра. '
                  f'Тип: {type(exception).__name__}. Секреты и клиентские данные в уведомление не включены.', now)

    def payments(self, now=None):
        now = int(time.time() if now is None else now)
        for company in self.store.list_companies():
            end = company['expires_at']
            if company['perpetual'] or not company['enabled'] or end is None or end > now:
                continue
            date = datetime.fromtimestamp(end, ZoneInfo('Asia/Dubai')).strftime('%d.%m.%Y %H:%M')
            self.once(f"expired:{company['id']}:{end}",
                      f"Просрочена оплата компании {company['id']}. Срок закончился {date} (Asia/Dubai). "
                      f"Доступ остановлен. Продлить: /renew {company['id']}", now)

    async def flush(self, bot, admin_id):
        with self.store.connection() as db:
            pending = db.execute('SELECT notice_key,text FROM owner_notices WHERE sent_at IS NULL '
                                 'ORDER BY created_at,notice_key LIMIT 20').fetchall()
        for notice in pending:
            await bot.send_message(admin_id, notice['text'])
            with self.store.connection() as db:
                db.execute('UPDATE owner_notices SET sent_at=? WHERE notice_key=?',
                           (int(time.time()), notice['notice_key']))

    async def run(self, bot, admin_id):
        while True:
            try:
                self.payments()
                await self.flush(bot, admin_id)
            except Exception as exc:
                # Do not notify through a failing notification channel recursively.
                log.warning('Owner notifications unavailable (%s); notices retained', type(exc).__name__)
            await asyncio.sleep(30)
