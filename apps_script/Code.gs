/**
 * Google Sheets -> server sync.
 *
 * Reads the sheet, skips the upload when nothing changed (SHA-256 of the data),
 * otherwise sends the rows gzip-compressed in chunks to the server.
 *
 * Script Properties (Project Settings -> Script Properties):
 *   SERVER_URL  e.g. https://dashboard.example.com
 *   SYNC_TOKEN  long random string, the same value as on the server
 *
 * No service account key is needed: the script runs as the sheet owner and pushes the data itself.
 */

const CONFIG = {
  SHEET_NAME: 'Leads',
  ROWS_PER_CHUNK: 20000,   // keeps each compressed request far below typical body limits
  TIMEZONE: 'Asia/Baku',
};

function syncToServer() {
  const props = PropertiesService.getScriptProperties();
  const serverUrl = props.getProperty('SERVER_URL');
  const token = props.getProperty('SYNC_TOKEN');
  if (!serverUrl || !token) throw new Error('Set SERVER_URL and SYNC_TOKEN in Script Properties.');

  const sheet = SpreadsheetApp.getActive().getSheetByName(CONFIG.SHEET_NAME);
  const values = sheet.getDataRange().getValues();
  const columns = values.shift();
  const rows = values
    .filter(row => row.some(cell => cell !== ''))
    .map(row => row.map(normalize_));

  // 1. Change detection: same data -> no upload.
  const hash = sha256_(JSON.stringify([columns, rows]));
  if (hash === props.getProperty('LAST_HASH')) {
    console.log('No changes since last sync, skipped.');
    return;
  }

  // 2. Chunked, compressed upload. The server swaps the table only after the last part arrives.
  const batchId = Utilities.getUuid();
  const totalParts = Math.max(1, Math.ceil(rows.length / CONFIG.ROWS_PER_CHUNK));
  for (let part = 0; part < totalParts; part++) {
    const chunk = rows.slice(part * CONFIG.ROWS_PER_CHUNK, (part + 1) * CONFIG.ROWS_PER_CHUNK);
    const json = JSON.stringify({ columns: columns, rows: chunk });
    const gz = Utilities.gzip(Utilities.newBlob(json, 'application/json'));
    const response = UrlFetchApp.fetch(serverUrl + '/api/upload', {
      method: 'post',
      contentType: 'application/gzip',
      payload: gz.getBytes(),
      headers: {
        'X-Sync-Token': token,
        'X-Batch-Id': batchId,
        'X-Part': String(part),
        'X-Total-Parts': String(totalParts),
      },
      muteHttpExceptions: true,
    });
    if (response.getResponseCode() !== 200) {
      throw new Error('Upload failed on part ' + part + ': ' + response.getContentText());
    }
  }

  // 3. Remember the hash only after every part was accepted.
  props.setProperty('LAST_HASH', hash);
  console.log('Synced ' + rows.length + ' rows in ' + totalParts + ' parts.');
}

/** Run once to create the 15-minute trigger. */
function installTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'syncToServer')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncToServer').timeBased().everyMinutes(15).create();
}

/** Run to force a full upload on the next sync. */
function resetHash() {
  PropertiesService.getScriptProperties().deleteProperty('LAST_HASH');
}

function normalize_(cell) {
  if (cell instanceof Date) return Utilities.formatDate(cell, CONFIG.TIMEZONE, 'yyyy-MM-dd');
  return cell;
}

function sha256_(text) {
  const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8);
  return bytes.map(b => ('0' + (b & 0xff).toString(16)).slice(-2)).join('');
}
