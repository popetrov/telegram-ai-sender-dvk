"""Last queue and socket-write witness for the pinned Telethon sender.

socket_write_attempt means a writer call, never a server acknowledgement.
No packet bytes, auth keys, or serialized Telegram payloads are stored.
"""
import hashlib

def install(client,sender):
    if getattr(sender,'_dvk_forensic_guard',False):return
    queue=getattr(sender,'_send_queue',None);state=getattr(sender,'_state',None)
    if queue is None or state is None:return  # synthetic test sender; startup checks real coverage
    original_get=queue.get;original_encrypt=state.encrypt_message_data
    batches={'current':[],'encrypted':{}}
    def connection_guard():
        conn=getattr(sender,'_connection',None)
        if conn is None or getattr(conn,'_dvk_forensic_guard',False):return
        original=conn._send
        def write(data):
            from forensic_trace import emit
            requests=batches['encrypted'].pop(hashlib.sha256(data).digest(),[])
            for request,mid in requests:client._trace(request,'socket_write_attempt',wire_message_id=mid)
            try:return original(data)
            except BaseException:
                for request,mid in requests:client._trace(request,'socket_write_unknown',wire_message_id=mid)
                raise
        conn._send=write;conn._dvk_forensic_guard=True
    async def get():
        batch,data=await original_get();requests=[]
        def visit(member):
            if isinstance(member,list):
                for x in member:visit(x)
                return
            request=getattr(member,'request',None)
            if request is None:return
            # RequestState stores serialized bytes at enqueue time. Inspect
            # those exact bytes too, not a mutable Python object alone.
            raw=getattr(member,'data',None)
            try:
                if isinstance(raw,bytes):
                    from telethon.extensions import BinaryReader
                    from safe_telegram import requests_in
                    try:
                        with BinaryReader(raw) as reader:decoded=reader.tgread_object()
                    except Exception:client._deny('unparseable_transport_request')
                    for old,new in zip(requests_in(request),requests_in(decoded)):
                        for attribute in ('_dvk_operation_id','_dvk_origin','_dvk_service_grant'):
                            if hasattr(old,attribute):setattr(new,attribute,getattr(old,attribute))
                    request=decoded
                client._check_request(request)
            except Exception as exc:
                future=getattr(member,'future',None)
                if future is not None and not future.done():future.set_exception(exc)
                raise
            if client._traceable(request):requests.append((request,int(getattr(member,'msg_id',0))))
        try:
            for member in batch:visit(member)
        except Exception as exc:
            # Telethon has already dequeued this complete batch. Resolve every
            # waiter; otherwise innocent sibling requests hang indefinitely.
            def fail(member):
                if isinstance(member,list):
                    for child in member:fail(child)
                else:
                    future=getattr(member,'future',None)
                    if future is not None and not future.done():future.set_exception(exc)
            for member in batch:fail(member)
            batches['current']=[]
            # Empty batch keeps Telethon's sender alive for signed incident
            # delivery. Business waiters still receive the blocking exception.
            return [],b''
        batches['current']=requests;connection_guard()
        return batch,data
    def encrypt(data):
        result=original_encrypt(data)
        if batches['current']:
            if len(batches['encrypted'])>=64:batches['encrypted'].pop(next(iter(batches['encrypted'])))
            batches['encrypted'][hashlib.sha256(result).digest()]=batches['current']
        batches['current']=[]
        return result
    queue.get=get;state.encrypt_message_data=encrypt;sender._dvk_forensic_guard=True
