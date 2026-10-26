"""Persistent safety latch and durable notifications; no customer data."""
import asyncio
import base64
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from access_lease import AccessDenied, canonical

STOP_TEXT = ('Защита остановила программу: обнаружена попытка удалить сообщения или изменить историю Telegram. '
             'Этот запрос заблокирован до отправки в Telegram. Рассылка и автоответы приостановлены, '
             'очередь сохранена. Перезапуск не снимает остановку. Передайте владельцу код события.')


class SafetyStop(AccessDenied):
    pass


class SafetyJournal:
    def __init__(self, path):
        self.path = Path(path)
        self._volatile = None

    def remember(self, operation):
        if self._volatile is None:
            self._volatile = dict(slot=1, incident_id=uuid.uuid4().hex, operation=operation,
                                  created_at=int(time.time()), manager_notified=0, owner_queued=0)
        return self._volatile

    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SafetyStop('Небезопасный файл защиты. Рассылка остановлена.')
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS safety_incident (
                slot INTEGER PRIMARY KEY CHECK(slot=1), incident_id TEXT NOT NULL,
                operation TEXT NOT NULL, created_at INTEGER NOT NULL,
                manager_notified INTEGER NOT NULL DEFAULT 0, owner_queued INTEGER NOT NULL DEFAULT 0)''')
        except BaseException:
            db.close()
            raise
        return db

    def current(self):
        if self._volatile is not None:
            return dict(self._volatile)
        if self.path.is_symlink():
            return self.remember('safety_journal_unreadable')
        if not self.path.exists():
            return None
        try:
            db = self.connection()
            try:
                row = db.execute('SELECT * FROM safety_incident WHERE slot=1').fetchone()
                if not row:
                    raise SafetyStop('Incomplete safety journal')
                return dict(row)
            finally:
                db.close()
        except (sqlite3.Error, OSError, SafetyStop):
            return self.remember('safety_journal_unreadable')

    def block(self, operation):
        operation = operation if re.fullmatch(r'[A-Za-z0-9_.]{1,100}', operation) else 'unknown'
        try:
            db = self.connection()
        except Exception:
            return self.remember(operation)
        try:
            with db:
                db.execute('INSERT OR IGNORE INTO safety_incident(slot,incident_id,operation,created_at) '
                           'VALUES (1,?,?,?)', (uuid.uuid4().hex, operation, int(time.time())))
            return dict(db.execute('SELECT * FROM safety_incident WHERE slot=1').fetchone())
        except Exception:
            return self.remember(operation)
        finally:
            db.close()

    def require_clear(self):
        row = self.current()
        if row:
            raise SafetyStop(notice_text(row) + ' Код: ' + row['incident_id'])

    def delivered(self, incident_id, field):
        if field not in ('manager_notified', 'owner_queued'):
            raise ValueError('Invalid notification field')
        if self._volatile is not None and self._volatile['incident_id']==incident_id:
            self._volatile[field]=1
            return
        db = self.connection()
        try:
            with db:
                db.execute(f'UPDATE safety_incident SET {field}=1 WHERE incident_id=?', (incident_id,))
        finally:
            db.close()


_DEFAULT_JOURNAL = SafetyJournal(Path(__file__).resolve().parent / 'data/telegram-safety.sqlite')


def default_journal():
    return _DEFAULT_JOURNAL


def notice_text(row):
    if row['operation']=='safety_journal_unreadable':
        return ('Защита остановила рассылку и автоответы: журнал безопасности повреждён или недоступен. '
                'Проверить безопасность запуска не удалось. Это не подтверждение удаления переписки. '
                'Нужна проверка владельца, очередь сохранена.')
    return STOP_TEXT


def validate_report(payload):
    if (not isinstance(payload, dict) or set(payload) != {'protocol', 'incident_id', 'manager_id', 'operation'}
            or payload['protocol'] != 'DVK_SAFETY_V1'
            or not isinstance(payload['incident_id'], str) or not re.fullmatch('[0-9a-f]{32}', payload['incident_id'])
            or not isinstance(payload['manager_id'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', payload['manager_id'])
            or not isinstance(payload['operation'], str) or not re.fullmatch('[A-Za-z0-9_.]{1,100}', payload['operation'])):
        raise ValueError('Invalid safety report')
    return payload


def register_receiver(application, store, signing_key, notices):
    from telegram.ext import CommandHandler

    async def receive(update, context):
        if update.effective_chat.type != 'private' or update.effective_user.is_bot:
            return
        try:
            if len(context.args) != 1 or len(context.args[0]) > 1200:
                raise ValueError('Invalid safety request')
            report = validate_report(json.loads(base64.urlsafe_b64decode(context.args[0])))
            account = update.effective_user.id
            subject = report['manager_id'] + ':' + str(account)
            with store.connection() as db:
                manager = db.execute('SELECT subject FROM managers WHERE account_id=?', (account,)).fetchone()
            if not manager or manager['subject'] != subject or notices is None:
                return
            # Stop other connected copies of this identity on their next lease check.
            store.revoke(account)
            from owner_notifications import manager_label
            notices.once('telegram-safety:' + str(account) + ':' + report['incident_id'],
                         'Защита переписки сработала. Менеджер: ' + manager_label(store, account) + '. '
                         + notice_text(report) + ' '
                         'Центральный доступ менеджера отозван до проверки. '
                         'Это подтверждает блокировку данного запроса, а не состояние всех чатов. '
                         'Нужна проверка владельца. Код: ' + report['incident_id'])
            payload = dict(protocol='DVK_SAFETY_ACK_V1', account_id=account,
                           manager_id=report['manager_id'], incident_id=report['incident_id'])
            signature = base64.b64encode(signing_key.sign(canonical(payload))).decode()
            await update.effective_message.reply_text('DVK_SAFETY_ACK ' + json.dumps(
                dict(payload=payload, signature=signature), separators=(',', ':')), disable_notification=True)
        except (ValueError, TypeError, KeyError):
            return

    application.add_handler(CommandHandler('safety', receive))


class SafetyNotifications:
    def __init__(self, client, notify_manager, account_id, manager_id, authority, public_key):
        self.client, self.notify_manager, self.account_id = client, notify_manager, account_id
        self.manager_id, self.authority, self.public_key = manager_id, authority, public_key
        self.journal = client.safety_journal

    async def step(self):
        row = self.journal.current()
        if not row:
            return
        if not row['manager_notified']:
            try:
                await self.notify_manager(notice_text(row) + '\nКод: ' + row['incident_id']
                                          + '\nУведомление владельцу будет передано при доступности центра.')
                self.journal.delivered(row['incident_id'], 'manager_notified')
            except Exception:
                pass
        if not row['owner_queued']:
            report = validate_report(dict(protocol='DVK_SAFETY_V1', incident_id=row['incident_id'],
                                          manager_id=self.manager_id, operation=row['operation']))
            await self.client.report_safety(self.authority, report, self.account_id, self.public_key)
            self.journal.delivered(row['incident_id'], 'owner_queued')

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception as exc:
                print('Уведомление защиты ожидает доставки (' + type(exc).__name__ + ').', flush=True)
            await asyncio.sleep(10)
