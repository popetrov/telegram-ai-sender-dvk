"""Owner-only roster. Never part of a lease or the manager sync protocol."""
import json
import re
import sqlite3

FIELDS=('full_name','number','department','company_name')
PROMPTS=('Введите ФИО для внутренней таблицы:',
         'Введите внутренний номер, например 008 (ведущие нули сохраняются):',
         'Введите направление, например Финанс, Право или Закупки:',
         'Введите название компании для таблицы, например ДВК:')


def clean(field,value):
    value=value.strip()
    if not value or len(value)>120 or any(ord(c)<32 for c in value):
        raise ValueError('Введите непустой текст до 120 символов без переносов строк.')
    if field=='number' and not re.fullmatch(r'[0-9]{1,12}',value):
        raise ValueError('Номер должен содержать только цифры, например 008.')
    return value


class ManagerDirectory:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS manager_directory (
                    account_id INTEGER PRIMARY KEY REFERENCES managers(account_id),
                    full_name TEXT NOT NULL, number TEXT NOT NULL UNIQUE,
                    department TEXT NOT NULL, company_name TEXT NOT NULL,
                    revision INTEGER NOT NULL, delivered_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS directory_drafts (
                    admin_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
                    step INTEGER NOT NULL, fields TEXT NOT NULL);
            ''')

    def profile(self,account_id):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM manager_directory WHERE account_id=?',(account_id,)).fetchone()
            return dict(row) if row else None

    def begin(self,admin_id,account_id):
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM managers WHERE account_id=? AND company_id IS NOT NULL',
                              (account_id,)).fetchone():
                raise ValueError('Сначала выдайте доступ: /approve ID КОМПАНИЯ')
            db.execute('INSERT INTO directory_drafts VALUES (?,?,0,?) ON CONFLICT(admin_id) '
                       'DO UPDATE SET account_id=excluded.account_id,step=0,fields=excluded.fields',
                       (admin_id,account_id,'{}'))
        return PROMPTS[0]

    def cancel(self,admin_id):
        with self.store.connection() as db:
            db.execute('DELETE FROM directory_drafts WHERE admin_id=?',(admin_id,))

    def answer(self,admin_id,text):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM directory_drafts WHERE admin_id=?',(admin_id,)).fetchone()
            if not row:
                return None
            fields=json.loads(row['fields']);step=row['step']
            fields[FIELDS[step]]=clean(FIELDS[step],text)
            if step<3:
                db.execute('UPDATE directory_drafts SET step=?,fields=? WHERE admin_id=?',
                           (step+1,json.dumps(fields,ensure_ascii=False),admin_id))
                return PROMPTS[step+1]
            try:
                db.execute('''INSERT INTO manager_directory
                    (account_id,full_name,number,department,company_name,revision)
                    VALUES (?,?,?,?,?,1) ON CONFLICT(account_id) DO UPDATE SET
                    full_name=excluded.full_name,number=excluded.number,
                    department=excluded.department,company_name=excluded.company_name,
                    revision=manager_directory.revision+1''',
                    (row['account_id'],*(fields[f] for f in FIELDS)))
            except sqlite3.IntegrityError as exc:
                raise ValueError('Этот номер уже занят. /cancel отменяет заполнение; /profile ID начинает заново.') from exc
            db.execute('DELETE FROM directory_drafts WHERE admin_id=?',(admin_id,))
        return ('Карточка сохранена только в центре. Менеджеру она не отправлялась. '
                'Запись в Google-таблицу выполнится автоматически после подключения таблицы; '
                'при недоступности Google карточка останется в очереди.')

    def pending(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM manager_directory '
                    'WHERE revision>delivered_revision ORDER BY account_id LIMIT 20')]

    def delivered(self,account_id,revision):
        with self.store.connection() as db:
            db.execute('UPDATE manager_directory SET delivered_revision=? WHERE account_id=? AND revision=?',
                       (revision,account_id,revision))


def register_admin_handlers(application,directory,admin_id):
    from telegram.ext import CommandHandler,MessageHandler,filters
    def owner(update):
        return update.effective_user.id==admin_id and update.effective_chat.type=='private'
    async def profile(update,context):
        if not owner(update): return
        try:
            if len(context.args)!=1 or not context.args[0].isdigit():
                raise ValueError('Формат: /profile ID')
            text=directory.begin(admin_id,int(context.args[0]))
        except ValueError as exc:
            text=str(exc)
        await update.effective_message.reply_text(text)
    async def cancel(update,context):
        if owner(update):
            directory.cancel(admin_id)
            await update.effective_message.reply_text('Заполнение карточки отменено. Выданный доступ не изменён.')
    async def answer(update,context):
        if not owner(update): return
        try:
            text=directory.answer(admin_id,update.effective_message.text)
        except ValueError as exc:
            text=str(exc)
        if text:
            await update.effective_message.reply_text(text)
    application.add_handler(CommandHandler('profile',profile))
    application.add_handler(CommandHandler('cancel',cancel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,answer))
