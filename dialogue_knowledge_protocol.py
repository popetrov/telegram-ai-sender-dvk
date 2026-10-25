"""Private, signed owner-side knowledge RPC over the existing technical bot.

Not a sync event: questions never enter business event replay or uploader ACKs.
The center validates the authenticated Telegram sender and current company
permission before touching its private knowledge provider.
"""
import base64
from dataclasses import asdict
import json
import time
import uuid
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from access_lease import canonical
from sync_protocol import encrypt,decrypt,public_text,event_digest,b64
from dialogue_knowledge import Evidence,KnowledgeAnswer,KnowledgeUnavailable

REQUEST_PROTOCOL='DVK_KNOWLEDGE_V1'
RESPONSE_PROTOCOL='DVK_KNOWLEDGE_RESULT_V1'


def make_request(center_public_key, *, subject,business_id,product,question,request_id=None,clock=time.time):
    private=X25519PrivateKey.generate()
    payload=dict(protocol=REQUEST_PROTOCOL,request_id=request_id or uuid.uuid4().hex,subject=subject,
                 business_id=business_id,product=product,question=question,
                 reply_public_key=public_text(private),created_at=int(clock()))
    return payload,private,encrypt(center_public_key,payload)


def validate_request(payload, account_id, *, clock=time.time):
    fields={'protocol','request_id','subject','business_id','product','question','reply_public_key','created_at'}
    if not isinstance(payload,dict) or set(payload)!=fields or payload['protocol']!=REQUEST_PROTOCOL:
        raise ValueError('Invalid knowledge protocol')
    if (not isinstance(payload['subject'],str) or len(payload['subject'])>200
            or not payload['subject'].endswith(':'+str(account_id))):
        raise PermissionError('Knowledge request account mismatch')
    if (not isinstance(payload['request_id'],str) or len(payload['request_id'])!=32
            or any(c not in '0123456789abcdef' for c in payload['request_id'])
            or type(payload['created_at']) is not int or abs(clock()-payload['created_at'])>600
            or any(not isinstance(payload[k],str) or not payload[k].strip() for k in ('question','product','business_id'))
            or len(payload['question'])>6000 or len(payload['product'])>100 or len(payload['business_id'])>100
            or len(base64.b64decode(payload['reply_public_key'],validate=True))!=32):
        raise ValueError('Invalid or expired knowledge request')


def response_packet(payload, *, signing_key,answer=None,error=None,clock=time.time):
    content=dict(protocol=RESPONSE_PROTOCOL,request_id=payload['request_id'],subject=payload['subject'],
                 request_digest=event_digest(payload),created_at=int(clock()),
                 answer=(dict(answer=answer.answer,evidence=[asdict(e) for e in answer.evidence]) if answer else None),
                 error=error)
    signed=dict(payload=content,signature=b64(signing_key.sign(canonical(content))))
    return encrypt(payload['reply_public_key'],signed)


def read_response(packet, *, request,private_key,signing_public_key,clock=time.time):
    signed=decrypt(private_key,packet)
    content=signed['payload']
    signing_public_key.verify(base64.b64decode(signed['signature'],validate=True),canonical(content))
    if (content['protocol']!=RESPONSE_PROTOCOL or content['request_id']!=request['request_id']
            or content['subject']!=request['subject'] or content['request_digest']!=event_digest(request)
            or type(content['created_at']) is not int or abs(clock()-content['created_at'])>300):
        raise ValueError('Knowledge result belongs to another request or expired')
    if content['error'] is not None:
        raise KnowledgeUnavailable('Center knowledge unavailable')
    value=content['answer']
    if (not isinstance(value,dict) or set(value)!={'answer','evidence'}
            or not isinstance(value['answer'],str) or not value['answer'].strip()
            or len(value['answer'])>6000 or not isinstance(value['evidence'],list)
            or not 1<=len(value['evidence'])<=10):
        raise KnowledgeUnavailable('Invalid center knowledge result')
    evidence=[]
    for row in value['evidence']:
        if (not isinstance(row,dict) or set(row)!={'id','file_id','filename','text'}
                or any(not isinstance(v,str) or not v for v in row.values()) or len(row['text'])>48000):
            raise KnowledgeUnavailable('Invalid center knowledge evidence')
        evidence.append(Evidence(**row))
    return KnowledgeAnswer(value['answer'],tuple(evidence),request['request_id'])


class KnowledgeBroker:
    def __init__(self, authority, providers,signing_key,encryption_key,clock=time.time):
        self.authority,self.providers=authority,providers
        self.signing_key,self.encryption_key=signing_key,encryption_key
        self.clock=clock

    def binding(self, payload, account_id):
        allowed,_=self.authority.permission(account_id,payload['subject'])
        if not allowed:
            raise PermissionError('Knowledge permission denied')
        manager=next((m for m in self.authority.list_managers() if m['account_id']==account_id),None)
        if manager is None:
            raise PermissionError('Unknown manager')
        key=(manager['company_id'],payload['business_id'])
        provider=self.providers.get(key)
        if provider is None or payload['product'] not in provider.products:
            raise PermissionError('Knowledge business binding denied')
        return provider

    async def handle(self, packet, *, account_id):
        payload=decrypt(self.encryption_key,packet)
        validate_request(payload,account_id,clock=self.clock)
        try:
            provider=self.binding(payload,account_id)
            answer=await provider.lookup(business_id=payload['business_id'],product=payload['product'],
                                         question=payload['question'],request_id=payload['request_id'])
            # Revocation/expiry during an AI call also blocks the result.
            self.binding(payload,account_id)
            return response_packet(payload,signing_key=self.signing_key,answer=answer,clock=self.clock)
        except (PermissionError,KnowledgeUnavailable):
            return response_packet(payload,signing_key=self.signing_key,error='unavailable',clock=self.clock)
