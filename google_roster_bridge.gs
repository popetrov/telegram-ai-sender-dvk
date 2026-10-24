// Bind this script to the owner's "Менеджеры бота" spreadsheet. No manager uses it.
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

function doPost(e) {
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
