/**
 * issue-sync trigger for Google Sheets.
 *
 * Notifies issue-sync whenever a registered worksheet is edited, so changes are synced
 * within seconds instead of waiting for the next poll. The request only names the
 * worksheet; issue-sync re-reads the content itself through the Sheets API.
 *
 * New rows typed by hand are taken once they are complete (required fields filled in)
 * and have not changed for NEW_ROW_SETTLE_SECONDS (default 2 minutes), so a row is never
 * triaged half-written. Rows written by a Google Form are taken right away: the form
 * writes the whole row at once. For requests from people outside the team, a Google
 * Form linked to the worksheet is the recommended way in.
 *
 * Setup (Extensions > Apps Script in the spreadsheet):
 *   1. Paste this file into Code.gs.
 *   2. Project Settings > Script properties: add
 *        ISSUE_SYNC_URL     https://<your-host>/webhooks/sheets
 *        ISSUE_SYNC_SECRET  <the SHEETS_WEBHOOK_SECRET value from .env>
 *   3. Run `installTriggers` once and accept the permission prompt. Run it again after
 *      linking a form, so the form trigger is added.
 *
 * Writes made by issue-sync through the API do not fire these triggers, so there is no
 * feedback loop.
 */

const HANDLERS = ['notifyIssueSync', 'notifyFormSubmit'];

function installTriggers() {
  const spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers()
    .filter((t) => HANDLERS.indexOf(t.getHandlerFunction()) !== -1)
    .forEach((t) => ScriptApp.deleteTrigger(t));
  // Installable triggers: simple onEdit triggers may not call external URLs.
  ScriptApp.newTrigger('notifyIssueSync').forSpreadsheet(spreadsheet).onEdit().create();
  // onChange also covers inserted / removed rows and pasted ranges.
  ScriptApp.newTrigger('notifyIssueSync').forSpreadsheet(spreadsheet).onChange().create();
  // Fires only for form submissions, never for typing: the row arrives complete.
  const hasForm = spreadsheet.getSheets().some((sheet) => sheet.getFormUrl());
  if (hasForm) {
    ScriptApp.newTrigger('notifyFormSubmit').forSpreadsheet(spreadsheet).onFormSubmit().create();
  }
}

function notifyIssueSync(e) {
  const spreadsheet = SpreadsheetApp.getActive();
  const sheet = e && e.range ? e.range.getSheet() : spreadsheet.getActiveSheet();
  post_({
    spreadsheet_id: spreadsheet.getId(),
    worksheet_name: sheet.getName(),
    source: e && e.changeType ? 'change' : 'edit',
  });
}

function notifyFormSubmit(e) {
  const spreadsheet = SpreadsheetApp.getActive();
  post_({
    spreadsheet_id: spreadsheet.getId(),
    worksheet_name: e.range.getSheet().getName(),
    source: 'form',
    row: e.range.getRow(),
  });
}

function post_(payload) {
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('ISSUE_SYNC_URL');
  const secret = props.getProperty('ISSUE_SYNC_SECRET');
  if (!url || !secret) {
    console.warn('ISSUE_SYNC_URL / ISSUE_SYNC_SECRET script properties are not set');
    return;
  }
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Issue-Sync-Secret': secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  // 404: this worksheet is not registered with issue-sync, which is fine.
  if (code !== 202 && code !== 404) {
    console.error('issue-sync returned ' + code + ': ' + response.getContentText());
  }
}
