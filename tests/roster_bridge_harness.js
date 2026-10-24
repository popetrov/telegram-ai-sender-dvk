// Exercise the actual Apps Script handler with an in-memory Sheets API stand-in.
const fs=require('fs'),vm=require('vm'),crypto=require('crypto'),assert=require('assert');
const secret='a'.repeat(64);
const rows=[['ФИО','Номер','организация','Компания','',''],['Existing Name','001','Финанс','ДВК','','']];
let literalWrites=0,hidden=false;
const sheet={
  getLastRow:()=>rows.length,getMaxRows:()=>1000,hideColumns:()=>{hidden=true;},
  getRange:(r,c,height=1,width=1)=>({
    getDisplayValues:()=>Array.from({length:height},(_,i)=>Array.from({length:width},(_,j)=>String(rows[r-1+i]?.[c-1+j]||''))),
    getFormulas:()=>Array.from({length:height},()=>Array(width).fill('')),
    setValues:values=>{for(let i=0;i<height;i++){rows[r-1+i]??=Array(6).fill('');for(let j=0;j<width;j++)rows[r-1+i][c-1+j]=values[i][j];}},
    setRichTextValues:values=>{literalWrites++;for(let i=0;i<height;i++){rows[r-1+i]??=Array(6).fill('');for(let j=0;j<width;j++)rows[r-1+i][c-1+j]=values[i][j].text;}}
  })
};
const context={
  PropertiesService:{getScriptProperties:()=>({getProperty:()=>secret})},
  Utilities:{Charset:{UTF_8:'utf8'},computeHmacSha256Signature:(message,key)=>[...crypto.createHmac('sha256',key).update(message).digest()]},
  ContentService:{MimeType:{JSON:'json'},createTextOutput:text=>({text,setMimeType(){return this;}})},
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  SpreadsheetApp:{openById:id=>{assert.equal(id,'1nmhAPOQV_EBUbv4rao8NSBpmvsEmEm8EIX4ep_8mrao');return {getSheetByName:name=>{assert.equal(name,'Лист1');return sheet;}};},
    newRichTextValue:()=>({setText(text){return {build:()=>({text})};}}),flush:()=>{}},
  Date,Number,JSON,Math,Error,String
};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
function invoke(profile,valid=true){
 const message=JSON.stringify({request_id:'b'.repeat(32),when:Math.floor(Date.now()/1000),profile});
 const signature=valid?crypto.createHmac('sha256',secret).update(message).digest('hex'):'c'.repeat(64);
 const outer=JSON.parse(context.doPost({postData:{contents:JSON.stringify({message,signature})}}).text);
 if(!outer.message)return outer;
 assert.equal(outer.signature,crypto.createHmac('sha256',secret).update(outer.message).digest('hex'));
 return JSON.parse(outer.message);
}
const profile={account_id:42,full_name:'Existing Name',number:'001',department:'Финанс',company_name:'ДВК',revision:1};
assert.equal(invoke(profile).ok,true);
assert.equal(rows.length,2);assert.equal(rows[1][1],'001');assert.equal(rows[1][4],'42');assert(hidden);
assert.equal(invoke(profile).ok,true);assert.equal(rows.length,2);
assert.equal(invoke({...profile,full_name:'Updated Name',revision:2}).ok,true);
assert.equal(invoke(profile).ok,true);assert.equal(rows[1][0],'Updated Name');assert.equal(rows[1][5],'2');
assert.equal(invoke({...profile,account_id:43}).ok,false);assert.equal(rows.length,2);
assert.equal(invoke({...profile,number:'007',revision:3}).ok,false);assert.equal(rows[1][1],'001');
const before=JSON.stringify(rows);
assert.equal(invoke({...profile,full_name:'Intruder'},false).ok,false);assert.equal(JSON.stringify(rows),before);
assert.equal(invoke({...profile,account_id:44,full_name:'=IMPORTXML("x")',number:'002'}).ok,true);
assert.equal(rows.length,3);assert.equal(rows[2][0],'=IMPORTXML("x")');assert(literalWrites>0);
console.log('PASS: signed bridge, existing-row binding, retry dedup, literal writes, stale-revision guard, identity collision, invalid-signature rejection.');
