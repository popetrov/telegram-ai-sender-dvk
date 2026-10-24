"""Query official SpamBot after a send stop; never clear the stop or send to clients."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError

log=logging.getLogger(__name__)
MONTHS={name:i+1 for i,name in enumerate(('jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'))}


def parse_status(text):
    text=text.strip()
    folded=text.casefold()
    if (folded.startswith('good news, no limits are currently applied to your account')
            or folded.startswith('ваш аккаунт свободен от каких-либо ограничений')):
        return 'clear',None
    if not re.search(r'account is (?:now )?limited',folded):
        return 'unknown',None
    match=re.search(r'limited until (\d{1,2}) ([A-Za-z]{3}) (\d{4}), (\d{2}):(\d{2}) UTC',text,re.I)
    until=None
    if match:
        try:
            day,month,year,hour,minute=match.groups()
            until=datetime(int(year),MONTHS[month.lower()],int(day),int(hour),int(minute),tzinfo=timezone.utc).timestamp()
        except (ValueError,KeyError):
            pass
    return 'limited',until


class SpamMonitor:
    def __init__(self,client,notify,root=Path('data'),clock=time.time):
        self.client,self.notify,self.root,self.clock=client,notify,Path(root),clock
        self.flag=self.root/'send_spam_block.json'
        self.path=self.root/'spam_monitor.json'
        self.peer=None

    def save(self,state):
        self.root.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
        os.replace(temporary,self.path)

    async def query(self):
        # Resolve once, then reuse the cached input peer. Conversation responses
        # come only from this peer and after our new /start message.
        async with asyncio.timeout(50):
            if self.peer is None:
                self.peer=await self.client.get_input_entity('SpamBot')
            async with self.client.conversation(self.peer,timeout=45,exclusive=True) as conversation:
                sent=await conversation.send_message('/start',silent=True)
                response=await conversation.get_response(sent)
                return parse_status(response.raw_text or '')

    def notice(self,status,until):
        if status=='clear':
            return ('@SpamBot сообщает: ограничений нет. Это не гарантирует доступность отправки через API. '
                    'Локальная остановка сохранена, сообщения клиентам не отправляются. '
                    'Автоматического восстановления или запуска очереди нет.')
        if status=='limited':
            date=(' до '+datetime.fromtimestamp(until,ZoneInfo('Asia/Dubai')).strftime('%d.%m.%Y %H:%M')+' по Дубаю') if until else ''
            return '@SpamBot сообщает об ограничении'+date+'. Проверка будет повторена с паузой. Рассылка остаётся остановленной.'
        return 'Ответ @SpamBot не распознан. Проверьте статус вручную. Рассылка остаётся остановленной.'

    async def step(self):
        if not self.flag.exists():
            return  # No PeerFlood incident: do not message SpamBot proactively.
        incident=hashlib.sha256(self.flag.read_bytes()).hexdigest()
        state=json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
        if state.get('incident')!=incident:
            state=dict(incident=incident,checks=0,next_check=0,status=None)
        now=self.clock()
        if state.get('status')!='clear' and now>=state.get('next_check',0):
            state['checks']+=1
            try:
                status,until=await self.query()
                finished=self.clock()
                token=f'{status}:{until}'
                if token!=state.get('notice_token'):
                    state['notice_token']=token
                    state['pending_notice']=self.notice(status,until)
                state.update(status=status,until=until,last_check=finished)
                # No rapid /start loop. Respect a stated restriction deadline;
                # otherwise check once a minute initially, then every 5 minutes.
                delay=60 if state['checks']<3 else 300
                state['next_check']=max(finished+delay,(until+5) if until and until>finished else 0)
                state.pop('error_type',None)
            except FloodWaitError as exc:
                state.update(next_check=self.clock()+max(60,int(exc.seconds)+5),error_type='FloodWaitError')
            except Exception as exc:
                state.update(next_check=self.clock()+300,error_type=type(exc).__name__)
                log.warning('SpamBot check unavailable (%s); send stop retained',type(exc).__name__)
            self.save(state)
        if state.get('pending_notice'):
            # Re-read the incident before publishing a delayed status. A manually
            # cleared/newer block must not receive an obsolete notification.
            if not self.flag.exists() or hashlib.sha256(self.flag.read_bytes()).hexdigest()!=incident:
                return
            await self.notify(state['pending_notice'])
            state.pop('pending_notice',None)
            self.save(state)

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception as exc:
                log.warning('SpamBot monitor unavailable (%s); no send permission changed',type(exc).__name__)
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(5)


async def start(client,notify,root=Path('data')):
    return asyncio.create_task(SpamMonitor(client,notify,root).run(),name='spam-status-monitor')
