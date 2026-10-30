"""Account-scoped incident correlation; no actor attribution from timing alone."""
import collections,json,time

def build(report):
    events=report['events'];by_id={};ops=collections.defaultdict(list);snapshots=[];chains=collections.defaultdict(list)
    for entry in events:
        r=entry['event'];evidence=r.get('evidence',{});kind=r['kind']
        if kind=='installation_snapshot':snapshots.append(entry)
        if evidence.get('op_id'):ops[evidence['op_id']].append(entry)
        if r.get('event_hash'):chains[entry['installation']].append(r)
        if kind in ('telegram_deleted','history_missing') and (kind!='telegram_deleted' or r.get('source')=='tracked_private_history'):
            for mid in r.get('message_ids',[]):
                item=by_id.setdefault(mid,dict(message_id=mid,observations=[],matched_operations=[],conclusion='source_not_established'))
                item['observations'].append(entry)
    for op_id,rows in ops.items():
        target_ids={mid for row in rows for mid in row['event'].get('evidence',{}).get('target_ids',[])}
        target_ids.update(mid for row in rows if row['event']['kind']=='send_result' for mid in row['event'].get('message_ids',[]))
        # Sent result IDs identify provenance; they do not mean an operation deleted them.
        for mid in target_ids & by_id.keys():
            relevant=[x for x in rows if x['event'].get('stage')=='blocked_before_network']
            destructive=any(x['event'].get('operation','').startswith(('Delete','Destroy','Clear','Discard')) for x in rows)
            by_id[mid]['matched_operations'].append(dict(op_id=op_id,events=rows,blocked=bool(relevant),destructive=destructive))
            if relevant and destructive:by_id[mid]['conclusion']='program_attempt_prevented_actual_actor_unknown'
    integrity=[]
    for installation,rows in chains.items():
        rows.sort(key=lambda r:r.get('local_sequence',0));gaps=[];broken=[]
        for previous,current in zip(rows,rows[1:]):
            if current['local_sequence']!=previous['local_sequence']+1:gaps.append([previous['local_sequence'],current['local_sequence']])
            elif current.get('previous_hash')!=previous.get('event_hash'):broken.append(current['local_sequence'])
        integrity.append(dict(installation=installation,available_from=rows[0]['local_sequence'],available_through=rows[-1]['local_sequence'],gaps=gaps,broken_links=broken))
    return dict(organization=report.get('organization',{}),protocol='DVK_FORENSIC_CASES_V1',account_id=report['account_id'],manager=report['manager'],
                company_id=report['company_id'],generated_at=int(time.time()),cases=list(by_id.values()),
                installations=snapshots,chain_coverage=integrity,possibly_truncated=report.get('possibly_truncated',False),
                rejected_events=report.get('rejected_events',0),
                limits='Blocked attempts did not delete messages. Unobserved copies and external actors are not excluded. Telegram does not supply the private deletion actor.')
