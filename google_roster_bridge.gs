// Bind this script to the owner's "Менеджеры бота" spreadsheet. No manager uses it.
// Application bootstrap stores only public request keys and encrypted replies.
function dvkAppJson(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
function dvkAppRequestValid(r) {
  return r && Object.keys(r).sort().join('|') === 'manager_id|nonce|protocol|public_key|request_id' &&
    r.protocol === 'DVK_APP_REQUEST_V1' && /^[a-f0-9]{32}$/.test(r.request_id) &&
    /^[a-f0-9]{32}$/.test(r.nonce) && /^[A-Za-z0-9+/]{43}=$/.test(r.public_key) &&
    /^[A-Za-z0-9_-]{1,80}$/.test(r.manager_id);
}
function dvkAppOwnerResponse(secret, requestId, ok, data) {
  const message = JSON.stringify({request_id:requestId, ok:ok, data:data});
  return dvkAppJson({message:message,signature:dvkMac(secret,message)});
}
function doPost(e) {
  let envelope;
  try {
    if (!e.postData || e.postData.contents.length > 20000) throw Error('invalid');
    envelope = JSON.parse(e.postData.contents);
    const cache = {
      get: key => CacheService.getScriptCache().get(key),
      put: (key,value,ttl) => CacheService.getScriptCache().put(key,value,ttl)
    };
    if (envelope.action === 'app_register') {
      if (!dvkAppRequestValid(envelope.request)) throw Error('invalid');
      const lock = LockService.getScriptLock();
      if (!lock.tryLock(1000)) return dvkAppJson({ok:false});
      try {
        const r = envelope.request, key = 'dvk-app-request:'+r.request_id;
        const prior = cache.get(key);
        if (prior) {
          if (prior !== JSON.stringify(r)) throw Error('conflict');
          return dvkAppJson({ok:true});
        }
        const bucket='dvk-app-rate:'+Math.floor(Date.now()/60000);
        const count=Number(cache.get(bucket)||'0');
        if (count>=60) throw Error('busy');
        cache.put(bucket,String(count+1),120);
        cache.put(key,JSON.stringify(r),1200);
        return dvkAppJson({ok:true});
      } finally {lock.releaseLock();}
    }
    if (envelope.action === 'app_poll') {
      if (!/^[a-f0-9]{32}$/.test(envelope.request_id)) throw Error('invalid');
      const packet=cache.get('dvk-app-response:'+envelope.request_id);
      return dvkAppJson({ok:true,packet:packet ? JSON.parse(packet) : null});
    }
    if (typeof envelope.message === 'string') {
      let r;
      try {r=JSON.parse(envelope.message);} catch (_) {r=null;}
      if (r && r.protocol === 'DVK_APP_BROKER_V1') {
        const secret=PropertiesService.getScriptProperties().getProperty('DVK_SECRET');
        if (!secret || !/^[a-f0-9]{64}$/.test(envelope.signature)) throw Error('invalid');
        const actual=dvkMac(secret,envelope.message);let diff=0;
        for(let i=0;i<actual.length;i++)diff|=actual.charCodeAt(i)^envelope.signature.charCodeAt(i);
        if(diff || !Number.isSafeInteger(r.when) || Math.abs(Date.now()/1000-r.when)>600 ||
          !/^[a-f0-9]{32}$/.test(r.request_id) || !/^[a-f0-9]{32}$/.test(r.installation)) throw Error('invalid');
        if(r.operation==='get_request') {
          const found=cache.get('dvk-app-request:'+r.installation);
          return dvkAppOwnerResponse(secret,r.request_id,!!found,found?JSON.parse(found):null);
        }
        if(r.operation==='put_response') {
          const found=cache.get('dvk-app-request:'+r.installation);
          if(!found || !r.packet || r.packet.protocol!=='DVK_APP_SEALED_V1' || JSON.stringify(r.packet).length>16000)
            return dvkAppOwnerResponse(secret,r.request_id,false,null);
          cache.put('dvk-app-response:'+r.installation,JSON.stringify(r.packet),900);
          return dvkAppOwnerResponse(secret,r.request_id,true,null);
        }
        return dvkAppOwnerResponse(secret,r.request_id,false,null);
      }
    }
  } catch (_) {return dvkAppJson({ok:false});}
  return dvkRosterPost(e);
}
const DVK_SHEET_ID = '1nmhAPOQV_EBUbv4rao8NSBpmvsEmEm8EIX4ep_8mrao';
const DVK_TAB = 'Лист1';

function setupDvkSync() {
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('DVK_SECRET')) {
    props.setProperty('DVK_SECRET', (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, ''));
  }
  const secret = props.getProperty('DVK_SECRET');
  const html = HtmlService.createHtmlOutput(
    '<p>Скопируйте секрет в локальную настройку центра. Не отправляйте его в чат.</p>' +
    '<textarea readonly style="width:100%;height:65px">' + secret + '</textarea>');
  SpreadsheetApp.getUi().showModalDialog(html, 'Подключение центра ДВК');
}

function dvkMac(secret, message) {
  return Utilities.computeHmacSha256Signature(message, secret, Utilities.Charset.UTF_8)
    .map(x => ('0' + ((x + 256) % 256).toString(16)).slice(-2)).join('');
}

function dvkResponse(secret, request, ok, code) {
  const message = JSON.stringify({request_id: request.request_id,
    account_id: request.profile.account_id, revision: request.profile.revision,
    ok: ok, code: code});
  return ContentService.createTextOutput(JSON.stringify({message: message, signature: dvkMac(secret, message)}))
    .setMimeType(ContentService.MimeType.JSON);
}

function dvkRosterPost(e) {
  const secret = PropertiesService.getScriptProperties().getProperty('DVK_SECRET');
  let request;
  try {
    if (!secret || !e.postData || e.postData.contents.length > 10000) throw Error('invalid');
    const envelope = JSON.parse(e.postData.contents);
    if (typeof envelope.message !== 'string' || !/^[a-f0-9]{64}$/.test(envelope.signature)) throw Error('invalid');
    const actual = dvkMac(secret, envelope.message);
    let diff = 0;
    for (let i = 0; i < actual.length; i++) diff |= actual.charCodeAt(i) ^ envelope.signature.charCodeAt(i);
    if (diff) throw Error('invalid');
    request = JSON.parse(envelope.message);
    const p = request.profile;
    if (!Number.isSafeInteger(request.when) || Math.abs(Date.now() / 1000 - request.when) > 600 || !/^[a-f0-9]{32}$/.test(request.request_id) ||
        !Number.isSafeInteger(p.account_id) || p.account_id <= 0 ||
        !Number.isSafeInteger(p.revision) || p.revision <= 0 || !/^[0-9]{1,12}$/.test(p.number)) throw Error('invalid');
    for (const field of ['full_name','department','company_name']) {
      if (typeof p[field] !== 'string' || !p[field].trim() || p[field].length > 120 || /[\x00-\x1f]/.test(p[field])) throw Error('invalid');
    }
  } catch (_) {
    return ContentService.createTextOutput('{"ok":false}').setMimeType(ContentService.MimeType.JSON);
  }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return dvkResponse(secret, request, false, 'busy');
  try {
    const sheet = SpreadsheetApp.openById(DVK_SHEET_ID).getSheetByName(DVK_TAB);
    if (!sheet) throw Error('sheet');
    const count = Math.max(1, sheet.getLastRow());
    const data = sheet.getRange(1, 1, count, 6).getDisplayValues();
    if (sheet.getRange(1,5,count,2).getFormulas().some(row => row.some(value => value))) throw Error('column_formula');
    if (data[0].slice(0,4).join('|') !== 'ФИО|Номер|организация|Компания') throw Error('headers');
    // Refuse to replace a user-owned fifth column. The hidden ID is deduplication only.
    if (data[0][4] !== '_telegram_id' && data.some(row => row[4] !== '')) throw Error('column');
    if (data[0][5] !== '_revision' && data.some(row => row[5] !== '')) throw Error('column');
    const p = request.profile, id = String(p.account_id);
    const ids = [], numbers = [];
    for (let i = 1; i < data.length; i++) {
      if (data[i][4] === id) ids.push(i);
      if (data[i][1] === p.number) numbers.push(i);
    }
    if (ids.length > 1 || numbers.length > 1) throw Error('duplicate');
    let row = ids.length ? ids[0] : -1;
    if (row !== -1 && data[row][1] !== p.number) throw Error('immutable_number');
    if (numbers.length && numbers[0] !== row) {
      const existing = numbers[0];
      if (row !== -1 || data[existing][4] || data[existing][0].trim() !== p.full_name.trim()) throw Error('conflict');
      row = existing; // Explicit existing number + exact owner-supplied name claims the row.
    }
    // An earlier HTTP request can arrive after a timeout/retry. Never replace
    // a newer revision with an older profile, even if arrival order changed.
    if (row !== -1 && data[row][4] === id && Number(data[row][5]) > p.revision)
      return dvkResponse(secret, request, true, 'newer_already_saved');
    if (row === -1) row = count; // zero-based insertion at the first row after the data.
    if (row + 1 > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), row + 1 - sheet.getMaxRows());
    // Write literal rich text: owner input can never become a spreadsheet formula.
    sheet.getRange(1,5,1,2).setValues([['_telegram_id','_revision']]);
    const values = [p.full_name, p.number, p.department, p.company_name, id, String(p.revision)];
    sheet.getRange(row + 1, 1, 1, 6).setRichTextValues([
      values.map(value => SpreadsheetApp.newRichTextValue().setText(value).build())]);
    sheet.hideColumns(5,2);
    SpreadsheetApp.flush();
    const saved = sheet.getRange(row + 1,1,1,6).getDisplayValues()[0];
    if (saved.some((value,i) => value !== values[i])) throw Error('readback');
    return dvkResponse(secret, request, true, 'saved');
  } catch (_) {
    return dvkResponse(secret, request, false, 'review');
  } finally {
    lock.releaseLock();
  }
}
