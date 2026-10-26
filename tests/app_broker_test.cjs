const fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const cache=new Map(),secret='synthetic-secret';
let spreadsheetCalls=0;
const context={
  CacheService:{getScriptCache:()=>({get:k=>cache.get(k)||null,put:(k,v)=>cache.set(k,v)})},
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  PropertiesService:{getScriptProperties:()=>({getProperty:()=>secret})},
  ContentService:{MimeType:{JSON:'json'},createTextOutput:text=>({text,setMimeType(){return this}})},
  Utilities:{Charset:{UTF_8:'utf8'},computeHmacSha256Signature:(message,key)=>Array.from(crypto.createHmac('sha256',key).update(message).digest())},
  SpreadsheetApp:{openById:()=>{spreadsheetCalls++;throw Error('should not access sheet during bootstrap')}},
};
vm.createContext(context);vm.runInContext(fs.readFileSync('google_roster_bridge.gs','utf8'),context);
function post(body){return JSON.parse(context.doPost({postData:{contents:JSON.stringify(body)}}).text)}
function owner(operation,id,packet,wrong=false){
  const message=JSON.stringify({protocol:'DVK_APP_BROKER_V1',request_id:'c'.repeat(32),when:Math.floor(Date.now()/1000),installation:id,operation,packet});
  return post({message,signature:crypto.createHmac('sha256',wrong?'wrong':secret).update(message).digest('hex')});
}
const request={protocol:'DVK_APP_REQUEST_V1',request_id:'a'.repeat(32),nonce:'b'.repeat(32),public_key:Buffer.alloc(32,1).toString('base64'),manager_id:'manager_007'};
assert.equal(post({action:'app_register',request}).ok,true);
assert.equal(post({action:'app_register',request:{...request,manager_id:'manager_008'}}).ok,false);
assert.equal(post({action:'app_register',request:{...request,api_hash:'forbidden'}}).ok,false);
assert.deepEqual(post({action:'app_poll',request_id:request.request_id}),{ok:true,packet:null});
assert.equal(owner('put_response',request.request_id,{protocol:'DVK_APP_SEALED_V1',data:'encrypted'},true).ok,false);
const received=owner('get_request',request.request_id);
assert.equal(crypto.createHmac('sha256',secret).update(received.message).digest('hex'),received.signature);
assert.deepEqual(JSON.parse(received.message).data,request);
assert.equal(JSON.parse(owner('put_response',request.request_id,{protocol:'PLAINTEXT',api_hash:'no'}).message).ok,false);
const packet={protocol:'DVK_APP_SEALED_V1',data:'encrypted'};
assert.equal(JSON.parse(owner('put_response',request.request_id,packet).message).ok,true);
assert.deepEqual(post({action:'app_poll',request_id:request.request_id}).packet,packet);
assert.equal(spreadsheetCalls,0);
console.log('Encrypted bootstrap broker: request validation, immutable requests, owner HMAC and isolated cache passed.');
