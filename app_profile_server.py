"""Center-approved delivery; no plaintext application pair leaves the center."""
import asyncio,base64,json,re,time
from pathlib import Path
from telegram.ext import CommandHandler
from app_profile_protocol import validate_request,issue
from app_profile_broker import owner_call
from telegram_app_migrate import read_profile
from telegram_app_trust import PROFILE_SIGNING_PUBLIC_KEY

class AppProfileService:
    def __init__(self,store,key,state,notices=None):
        self.store,self.key,self.state,self.notices=store,key,Path(state),notices
        # Validate the private owner file without ever logging its contents.
        read_profile(self.state/'telegram-app-profile.private.json',PROFILE_SIGNING_PUBLIC_KEY)
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS app_bootstrap_requests (
                request_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, subject TEXT NOT NULL,
                request_json TEXT NOT NULL, expires_at INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)''')

    def profile(self):
        path=self.state/'telegram-app-profile.private.json'
        read_profile(path,PROFILE_SIGNING_PUBLIC_KEY)
        return json.loads(path.read_text('utf-8'))

    def settings(self):return json.loads((self.state/'sheets.json').read_text('utf-8'))

    def register(self,account,request):
        validate_request(request);subject=request['manager_id']+':'+str(account)
        self.store.register(account,subject)
        if self.notices:
            allowed,_=self.store.permission(account,subject)
            if not allowed:self.notices.once('request:'+subject,
                f'Новый менеджер: Telegram ID {account}. Ожидает подключения.\nРазрешить: /approve {account} КОМПАНИЯ')
        return subject

    async def direct(self,update,context):
        if update.effective_chat.type!='private' or update.effective_user.is_bot:return
        try:
            if len(context.args)!=1 or len(context.args[0])>1800:raise ValueError()
            request=validate_request(json.loads(base64.urlsafe_b64decode(context.args[0])))
            account=update.effective_user.id;subject=self.register(account,request)
            allowed,_=self.store.permission(account,subject)
            if not allowed:
                await update.effective_message.reply_text('DVK_APP_PENDING',disable_notification=True);return
            packet=issue(self.key,request,account,self.profile())
            text='DVK_APP '+json.dumps(packet,separators=(',',':'))
            if len(text)>4000:raise ValueError()
            await update.effective_message.reply_text(text,disable_notification=True)
        except Exception as exc:
            if self.notices:self.notices.error('настройка Telegram-приложения',exc,update.effective_user.id)
            await update.effective_message.reply_text('DVK_APP_UNAVAILABLE',disable_notification=True)

    async def join(self,update,context):
        if update.effective_chat.type!='private' or update.effective_user.is_bot:return
        try:
            if len(context.args)!=1 or not re.fullmatch('app_[a-f0-9]{32}',context.args[0]):return
            identity=context.args[0][4:];account=update.effective_user.id
            request=validate_request(await asyncio.to_thread(owner_call,self.settings(),'get_request',identity))
            if request['request_id']!=identity:raise ValueError()
            subject=self.register(account,request)
            with self.store.connection() as db:
                old=db.execute('SELECT account_id,request_json FROM app_bootstrap_requests WHERE request_id=?',(identity,)).fetchone()
                if old and (old['account_id']!=account or json.loads(old['request_json'])!=request):raise ValueError()
                db.execute('INSERT OR IGNORE INTO app_bootstrap_requests VALUES (?,?,?,?,?,0)',
                    (identity,account,subject,json.dumps(request),int(time.time())+1200))
            await update.effective_message.reply_text(
                'Подключение подтверждено. Вернитесь в программу на компьютере. '
                'Если доступ ещё не выдан, дождитесь решения руководителя.')
            await self.deliver()
        except Exception as exc:
            if self.notices:self.notices.error('первое подключение',exc,update.effective_user.id)
            await update.effective_message.reply_text('Не удалось подтвердить подключение. Откройте новую ссылку из программы и повторите.')

    async def deliver(self):
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM app_bootstrap_requests WHERE delivered=0 AND expires_at>? LIMIT 30',(int(time.time()),)).fetchall()
        for row in rows:
            allowed,_=self.store.permission(row['account_id'],row['subject'])
            if not allowed:continue
            packet=issue(self.key,json.loads(row['request_json']),row['account_id'],self.profile())
            await asyncio.to_thread(owner_call,self.settings(),'put_response',row['request_id'],packet)
            with self.store.connection() as db:db.execute('UPDATE app_bootstrap_requests SET delivered=1 WHERE request_id=?',(row['request_id'],))

    async def loop(self):
        while True:
            try:await self.deliver()
            except Exception as exc:
                if self.notices:self.notices.error('автонастройка Telegram',exc)
            await asyncio.sleep(10)

def register_handlers(application,service):
    application.add_handler(CommandHandler('app_profile',service.direct))
    application.add_handler(CommandHandler('start',service.join))
