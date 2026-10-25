"""Business-specific dialogue rules, without Telegram, AI or persistence."""
import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Handoff:
    notify: bool
    pause: bool
    label: str


class DialoguePolicy:
    def __init__(self, value):
        if not isinstance(value,dict) or value.get('schema_version') != 1:
            raise ValueError('Unsupported dialogue business package')
        self.business_id=value['business_id']
        self.products=tuple(value['products'])
        self.timezone=ZoneInfo(value['timezone'])
        self.enabled=value['enabled']
        self.mode=value['mode']
        self.model=value.get('model','gpt-4.1')
        self.prompts=value['prompts']
        self.acknowledgements=value.get('event_acknowledgements',{})
        self.apology=value['opt_out_apology']
        self.steps=tuple(value['silent_reminders'])
        self.knowledge=value['knowledge']
        self.handoffs={k:Handoff(**v) for k,v in value['handoffs'].items()}
        if (not isinstance(self.business_id,str) or not self.business_id.strip()
                or type(self.enabled) is not bool or self.mode not in {'automatic','draft'}
                or not self.products or any(not isinstance(p,str) or not p.strip() for p in self.products)
                or len(set(self.products))!=len(self.products)
                or not isinstance(self.apology,str) or not self.apology.strip() or len(self.apology)>4096):
            raise ValueError('Invalid dialogue package settings')
        if (not isinstance(self.model,str) or not self.model.strip() or not isinstance(self.prompts,dict)
                or set(self.prompts)!={'analyst','writer'}
                or any(not isinstance(v,str) or not v.strip() or len(v)>16000 for v in self.prompts.values())):
            raise ValueError('Invalid business dialogue instructions')
        if (not isinstance(self.acknowledgements,dict) or set(self.acknowledgements)-{'call','documents'}
                or any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in self.acknowledgements.values())):
            raise ValueError('Invalid handoff acknowledgements')
        if self.knowledge.get('adapter') not in {'owner_vector_store'}:
            raise ValueError('Unknown knowledge adapter')
        if not isinstance(self.knowledge.get('bot'),str) or not self.knowledge['bot'].endswith('_bot'):
            raise ValueError('Invalid knowledge bot')
        for step in self.steps:
            if (not isinstance(step,dict) or set(step)!={'unit','count'}
                    or step['unit'] not in {'days','months'} or type(step['count']) is not int
                    or not 1<=step['count']<=365):
                raise ValueError('Invalid follow-up interval')
        if len(self.steps)>12:
            raise ValueError('Too many follow-up stages')
        required={'ready','need','call','documents','knowledge_missing'}
        if not required.issubset(self.handoffs):
            raise ValueError('Missing handoff rules')
        if any(type(v.notify) is not bool or type(v.pause) is not bool or not v.label for v in self.handoffs.values()):
            raise ValueError('Invalid handoff rules')

    @classmethod
    def load(cls, path):
        path=Path(path)
        if path.is_symlink() or path.stat().st_size>128*1024:
            raise ValueError('Unsafe dialogue package')
        return cls(json.loads(path.read_text('utf-8-sig')))

    def next_reminder_at(self, first_sent_at, sent_reminders, *, has_reply=False):
        """Each interval starts at the preceding *successful* send, not at generation.

        An overdue schedule emits one step; it cannot catch up by sending three
        reminders together after a long outage. Calendar months keep local time.
        """
        times=[first_sent_at,*sent_reminders]
        if (any(type(t) is not int or t<=0 for t in times)
                or any(b<=a for a,b in zip(times,times[1:])) or len(sent_reminders)>len(self.steps)):
            raise ValueError('Invalid confirmed follow-up history')
        if has_reply or len(sent_reminders)==len(self.steps):
            return None
        interval=self.steps[len(sent_reminders)]
        previous=datetime.fromtimestamp(times[-1],self.timezone)
        if interval['unit']=='days':
            result=previous+timedelta(days=interval['count'])
        else:
            month_index=previous.year*12+previous.month-1+interval['count']
            year,month=divmod(month_index,12);month+=1
            result=previous.replace(year=year,month=month,
                                    day=min(previous.day,calendar.monthrange(year,month)[1]))
        return int(result.timestamp())

    def handoff(self,event):
        if event not in self.handoffs:
            raise ValueError('Unknown handoff event')
        return self.handoffs[event]
