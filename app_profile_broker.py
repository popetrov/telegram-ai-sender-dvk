"""The public broker stores only random requests and recipient-encrypted responses."""
import hashlib,hmac,json,re,time,urllib.request,uuid
from pathlib import Path
def signature(secret,message):return hmac.new(secret.encode(),message.encode('utf-8'),hashlib.sha256).hexdigest()

def validate_url(url):
    if not isinstance(url,str) or not re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec',url):
        raise ValueError('Invalid delivery service URL')
    return url

def post(url,body):
    request=urllib.request.Request(validate_url(url),data=json.dumps(body,separators=(',',':')).encode('utf-8'),
                                   headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request,timeout=25) as response:raw=response.read(32769)
    if len(raw)>32768:raise ValueError('Oversized delivery response')
    return json.loads(raw)

def register(url,request):
    result=post(url,dict(action='app_register',request=request))
    if result.get('ok') is not True:raise RuntimeError('Сервис подключения временно недоступен. Повторите позже.')

def poll(url,request_id):
    result=post(url,dict(action='app_poll',request_id=request_id))
    if result.get('ok') is not True:raise RuntimeError('Сервис подключения временно недоступен.')
    return result.get('packet')

def owner_call(settings,operation,request_id,packet=None):
    call_id=uuid.uuid4().hex
    payload=dict(protocol='DVK_APP_BROKER_V1',request_id=call_id,when=int(time.time()),operation=operation,installation=request_id)
    if packet is not None:payload['packet']=packet
    message=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    envelope=post(settings['url'],dict(message=message,signature=signature(settings['secret'],message)))
    signed=envelope['message']
    if not hmac.compare_digest(envelope['signature'],signature(settings['secret'],signed)):raise ValueError('Broker signature mismatch')
    result=json.loads(signed)
    if result.get('request_id')!=call_id or result.get('ok') is not True:raise ValueError('Broker did not confirm request')
    return result.get('data')
