/**
 * issue-sync trigger for Google Sheets.
 *
 * Notifies issue-sync whenever a registered worksheet is edited, so changes are synced
 * within seconds instead of waiting for the next poll. The request only names the
 * worksheet; issue-sync re-reads the content itself through the Sheets API.
 *
 * Setup (Extensions > Apps Script in the spreadsheet):
 *   1. Paste this file into Code.gs.
 *   2. Project Settings > Script properties: add
 *        ISSUE_SYNC_URL     https://<your-host>/webhooks/sheets
 *        ISSUE_SYNC_SECRET  <the SHEETS_WEBHOOK_SECRET value from .env>
 *   3. Run `installTriggers` once and accept the permission prompt.
 *
 * Writes made by issue-sync through the API do not fire these triggers, so there is no
 * feedback loop.
 */

function installTriggers() {
  const spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === 'notifyIssueSync')
    .forEach((t) => ScriptApp.deleteTrigger(t));
  // Installable triggers: simple onEdit triggers may not call external URLs.
  ScriptApp.newTrigger('notifyIssueSync').forSpreadsheet(spreadsheet).onEdit().create();
  // onChange also covers inserted / removed rows and pasted ranges.
  ScriptApp.newTrigger('notifyIssueSync').forSpreadsheet(spreadsheet).onChange().create();
}

function notifyIssueSync(e) {
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('ISSUE_SYNC_URL');
  const secret = props.getProperty('ISSUE_SYNC_SECRET');
  if (!url || !secret) {
    console.warn('ISSUE_SYNC_URL / ISSUE_SYNC_SECRET script properties are not set');
    return;
  }
  const spreadsheet = SpreadsheetApp.getActive();
  const sheet = e && e.range ? e.range.getSheet() : spreadsheet.getActiveSheet();
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Issue-Sync-Secret': secret },
    payload: JSON.stringify({
      spreadsheet_id: spreadsheet.getId(),
      worksheet_name: sheet.getName(),
    }),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  // 404: this worksheet is not registered with issue-sync, which is fine.
  if (code !== 202 && code !== 404) {
    console.error('issue-sync returned ' + code + ': ' + response.getContentText());
  }
}
