"""Read-only central telemetry export for incident investigations. No Telegram calls."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from incident_monitor import export_report


class ReadOnlyStore:
    def __init__(self,path):self.path=Path(path).resolve()
    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:db.close()


def analyze(report):
    attempts={};finished=set();runs={};deletions=[];blocked=[]
    for item in report['events']:
        event=item['event'];kind=event['kind'];run=event['run_id']
        state=runs.setdefault(run,dict(first_event=event['recorded_at'],last_event=event['recorded_at'],events=0))
        state['first_event']=min(state['first_event'],event['recorded_at'])
        state['last_event']=max(state['last_event'],event['recorded_at']);state['events']+=1
        if kind=='startup':
            state.update({key:event[key] for key in ('version','machine_id','folder_id','pid','files') if key in event})
        if kind=='send_attempt':attempts[item['event_id']]=item
        elif kind in ('send_result','send_error'):finished.add(event.get('request_id'))
        elif kind in ('telegram_deleted','history_missing'):
            deletions.append(dict(event_id=item['event_id'],kind=kind,message_ids=event.get('message_ids',[]),
                                  source=event.get('source'),run_id=run,recorded_at=event['recorded_at']))
        elif kind=='request_blocked':blocked.append(item['event_id'])
    return dict(runs=runs,unfinished_attempts=[eid for eid in attempts if eid not in finished],
                deletions=deletions,blocked_request_events=blocked,
                interpretation='Unfinished attempt does not prove a failed send. Never retry automatically. '
                'Deletion observation does not identify the actor. Request-blocked event proves only that '
                'this guarded request was stopped. Compare run, code hashes, timestamps and session snapshots.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--account',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=export_report(ReadOnlyStore(args.state/'authority.sqlite'),args.account)
    report['investigation']=analyze(report)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved metadata events: '+str(len(report['events'])))

if __name__=='__main__':main()
