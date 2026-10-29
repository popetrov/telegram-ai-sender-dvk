"""Auditable partial estimates, distinct from organization billing."""
import json
import re

SOURCE='https://developers.openai.com/api/docs/pricing'
CHECKED='2026-10-29'
# Standard processing, USD per million tokens. Unknown models stay unknown.
RATES={'gpt-4.1':(2,.5,8),'gpt-5-mini':(.25,.025,2)}

def estimate(row):
    result=dict(usd_min=None,usd_max=None,tools_complete=False,tariff_source=SOURCE,
                tool_usd_known=0.,billing_complete=False)
    model=re.sub(r'-\d{4}-\d{2}-\d{2}$','',row['model'])
    rates=RATES.get(model)
    if rates is None:return result
    inp,out=row.get('input_tokens'),row.get('output_tokens')
    if type(inp) is not int or type(out) is not int or min(inp,out)<0:return result
    try:data=json.loads(row.get('usage_json') or '{}')
    except (ValueError,TypeError):data={}
    cached=row.get('cached_tokens')
    if cached is None:cached=(data.get('usage',data).get('input_tokens_details') or {}).get('cached_tokens')
    pi,pc,po=rates
    if type(cached) is int and 0<=cached<=inp:lo=hi=(out*po+(inp-cached)*pi+cached*pc)/1e6
    else:lo,hi=(out*po+inp*pc)/1e6,(out*po+inp*pi)/1e6
    calls=data.get('tool_calls');tools=data.get('tools',[])
    # A configured tool is not proof that it was invoked.
    fee=0.;complete=isinstance(calls,list)
    for call in calls or []:
        if call=='web_search_call':
            fee+=.025 if 'web_search_preview' in tools and model=='gpt-4.1' else .01
        elif call=='file_search_call':fee+=.0025
        else:complete=False
    # Search-content billing, storage, regional/priority charges and outside
    # requests cannot be reconstructed from this local ledger.
    return dict(result,usd_min=lo,usd_max=hi,tool_usd_known=fee,tools_complete=complete)

def provider_costs(start,end,*,admin_key=None):
    """Read official billing only with an administrator credential, at center."""
    import os
    import urllib.parse
    import urllib.request
    key=admin_key or os.getenv('OPENAI_ADMIN_KEY','').strip()
    if not key:return dict(available=False,reason='not_configured')
    # Costs are daily UTC buckets. Do not imply precision for Dubai midnight.
    a=(start//86400)*86400;b=((end+86399)//86400)*86400
    entries=[];page=None;seen=set()
    for _ in range(100):
        query=dict(start_time=a,end_time=b,bucket_width='1d',limit=180)
        if page:query['page']=page
        request=urllib.request.Request('https://api.openai.com/v1/organization/costs?'+urllib.parse.urlencode(query),headers={'Authorization':'Bearer '+key})
        with urllib.request.urlopen(request,timeout=20) as response:data=json.load(response)
        entries.extend(data.get('data',[]))
        if not data.get('has_more'):break
        page=data.get('next_page')
        if not page or page in seen:raise ValueError('Incomplete billing pagination')
        seen.add(page)
    else:raise ValueError('Billing page limit')
    total=0.
    for bucket in entries:
        for item in bucket.get('results',[]):
            amount=item.get('amount',{})
            if str(amount.get('currency','')).lower()!='usd':raise ValueError('Unknown billing currency')
            total+=float(amount['value'])
    return dict(available=True,usd=total,start=a,end=b,scope='OpenAI organization, all projects/keys',source='https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs')
