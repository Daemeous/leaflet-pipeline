// ?? Config ????????????????????????????????????????????????????????????????????
const AUTHORISED_SHEET = "Authorised";
// Read from a Script Property rather than hardcoded, so this file stays a
// straight copy-paste across every deployment (same as it was before this
// check existed) — the one deployment-specific value lives in per-Sheet
// config instead of in code, the same way Authorised's contents do.
// Set it once per deployment: this script's Project Settings -> Script
// Properties -> add key GOOGLE_CLIENT_ID, value = this deployment's
// index.html window.MAP_CONFIG.GOOGLE_CLIENT_ID (not a secret, just an
// identity binding — https://console.cloud.google.com/apis/credentials).
// Tokens whose audience isn't this client are rejected below: without that
// check, a valid Google token minted for ANY other app (for the same user
// email) would pass verification here.
// Falls back to the shared client ID reused across every existing
// deployment (Stafford/Barnsley/Stone/Burton_Uttoxeter/St_Helens/South_Hams)
// so a freshly automated deployment works immediately without a manual
// Script Properties paste. Override the property per-deployment only if a
// given deployment ever needs its own separate OAuth client.
const GOOGLE_CLIENT_ID = PropertiesService.getScriptProperties().getProperty("GOOGLE_CLIENT_ID")
  || "580224381168-i67a13m72bvlpq8rtkhnjk15tic4k9e1.apps.googleusercontent.com";

// When true, isAuthorised() treats every signed-in Google account as
// authorised, skipping the Authorised-sheet lookup entirely. Meant ONLY for
// a public demo deployment where anyone should be able to try editing —
// set via Script Property (Project Settings -> Script Properties -> add
// key DEMO_MODE, value "true") on the demo's Apps Script project only.
// Leave unset (defaults to false) on every real deployment.
const DEMO_MODE = PropertiesService.getScriptProperties().getProperty("DEMO_MODE") === "true";

// Pending-changes queue: lets a signed-in-but-unauthorised person
// PROPOSE a change (goes to the Pending sheet, never touches Data) for an
// authorised editor to approve/deny. Not published as CSV.
const PENDING_SHEET = "Pending";
const BANNED_SHEET  = "Banned";     // emails blocked from proposing. Not published as CSV.
const MAX_PENDING_PER_SUBMITTER = 20; // outstanding (unreviewed) cap, per email
const DATA_SHEET_NAME 	 = "Data";
const CHANGELOG_SHEET 	 = "Changelog"; 	 // NOT published as CSV 	 keep it that way
const STATUS_COL 	 	 	 	= 6; 	 	 	 	 	 	 // F 	 Status
const PARTIAL_COL 	 	 	 = 9; 	 	 	 	 	 	 // I 	 partial_geometry
const STREET_COL 	 	 	 	= 1; 	 	 	 	 	 	 // A 	 Street
const WARD_COL 	 	 	 	 	= 4; 	 	 	 	 	 	 // D 	 Ward
const CHANGELOG_MAX_AGE_DAYS = 30; 	 	 	 // entries older than this get trimmed

// ?? POST handler ??????????????????????????????????????????????????????????????
function doPost(e) {
	 try {
	 	 const body = JSON.parse(e.postData.contents);
	 	 if (body.action === "verify") 	return handleVerify(body);
	 	 if (body.action === "update") 	return handleUpdate(body);
	 	 if (body.action === "partial") return handlePartial(body);
	 	 if (body.action === "revert") 	return handleRevert(body);
	 	 if (body.action === "history") return handleHistory(body);
	 	 if (body.action === "propose") return handlePropose(body);
	 	 if (body.action === "pendingList")   return handlePendingList(body);
	 	 if (body.action === "pendingReview") return handlePendingReview(body);
	 	 if (body.action === "ban")   return handleBan(body);
	 	 if (body.action === "unban") return handleUnban(body);
	 	 if (body.action === "pendingStatus") return handlePendingStatus(body);
	 	 if (body.action === "sheetInfo") 	return handleSheetInfo(body);
	 	 return jsonResp({ ok: false, error: "Unknown action" });
	 } catch(err) {
	 	 return jsonResp({ ok: false, error: err.message });
	 }
}

// ?? Verify ????????????????????????????????????????????????????????????????????
function handleVerify(body) {
	 const email = getVerifiedEmail(body);
	 if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
	 return jsonResp({ ok: true, authorised: isAuthorised(email), banned: isBanned(email), email });
}

// ?? Update status ?????????????????????????????????????????????????????????????
function handleUpdate(body) {
	 const email = getVerifiedEmail(body);
	 if (!email) 	 	 	 	 	 	 	 return jsonResp({ ok: false, error: "Invalid or expired token" });
	 if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

	 const row 	 	= parseInt(body.rowIndex, 10);
	 const status = body.newStatus;
	 const allowed = ["Complete", "In_Progress", "Planned", "Not_Started"];
	 if (!allowed.includes(status)) return jsonResp({ ok: false, error: "Invalid status: " + status });
	 if (isNaN(row) || row < 2) 	 	 return jsonResp({ ok: false, error: "Invalid row index" });

	 const sheet = getDataSheet();
	 if (row > sheet.getLastRow()) return jsonResp({ ok: false, error: "Row out of range" });
	 const prevStatus = sheet.getRange(row, STATUS_COL).getValue();

	 sheet.getRange(row, STATUS_COL).setValue(status);
	 logChange(sheet, row, "status", prevStatus, status, email, false);

	 return jsonResp({ ok: true });
}

// ?? Update partial geometry ???????????????????????????????????????????????????
function handlePartial(body) {
	 const email = getVerifiedEmail(body);
	 if (!email) 	 	 	 	 	 	 	 return jsonResp({ ok: false, error: "Invalid or expired token" });
	 if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

	 const row = parseInt(body.rowIndex, 10);
	 if (isNaN(row) || row < 2) return jsonResp({ ok: false, error: "Invalid row index" });

	 const pg = String(body.partialGeometry || "-");
	 if (!/^[-|:.\d a-zA-Z]+$/.test(pg)) return jsonResp({ ok: false, error: "Invalid partial geometry format" });

	 const sheet = getDataSheet();
	 if (row > sheet.getLastRow()) return jsonResp({ ok: false, error: "Row out of range" });
	 const prevPg = sheet.getRange(row, PARTIAL_COL).getValue();

	 sheet.getRange(row, PARTIAL_COL).setValue(pg);
	 logChange(sheet, row, "partial_geometry", prevPg, pg, email, false);

	 return jsonResp({ ok: true });
}

// ?? Revert (sitewide purge of one editor's standing changes) ??????????????????
// This is a BLUNT INSTRUMENT for sabotage recovery, not a per-edit undo.
//
// For every (row, field) combination that targetEditor has ever touched,
// check whether their change is still the CURRENT standing value (i.e. the
// most recent changelog entry for that row+field was made by them). If so,
// walk backward to find the last entry made by someone else, and restore
// that value. If targetEditor's edit has already been superseded by a later
// edit from someone else, that row+field is left untouched 	 it's not
// "theirs" anymore.
//
// Every row+field actually changed gets its own changelog entry, attributed
// to whoever clicked the button (email), tagged isRevert=true, so the purge
// itself is fully auditable and 	 if needed 	 itself purgeable.
function handleRevert(body) {
	 const email = getVerifiedEmail(body);
	 if (!email) 	 	 	 	 	 	 	 return jsonResp({ ok: false, error: "Invalid or expired token" });
	 if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

	 const targetEditor = String(body.targetEditor || "").toLowerCase().trim();
	 if (!targetEditor) return jsonResp({ ok: false, error: "Missing targetEditor" });

	 const log = getChangelogSheet();
	 const allRows = log.getDataRange().getValues();
	 // Columns: 0 timestamp | 1 rowIndex | 2 street | 3 ward | 4 field | 5 oldValue | 6 newValue | 7 editorEmail | 8 isRevert

	 // Group all entries by "rowIndex|field"
	 const groups = {}; // key -> array of entries in sheet order (already append-order)
	 for (let i = 1; i < allRows.length; i++) {
	 	 const r = allRows[i];
	 	 const key = r[1] + "|" + r[4];
	 	 if (!groups[key]) groups[key] = [];
	 	 groups[key].push({
	 	 	 timestamp: r[0], rowIndex: parseInt(r[1], 10), field: r[4],
	 	 	 oldValue: r[5], newValue: r[6], editor: String(r[7]).toLowerCase()
	 	 });
	 }

	 const sheet = getDataSheet();
	 const reverted = []; 	 // { rowIndex, field, restoredTo }
	 const skipped 	= []; 	 // rows where targetEditor's change was already superseded

	 Object.keys(groups).forEach(key => {
	 	 const entries = groups[key];
	 	 entries.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
	 	 const last = entries[entries.length - 1];

	 	 if (last.editor !== targetEditor) {
	 	 	 // Someone else's edit is the current standing value 	 not ours to touch
	 	 	 if (entries.some(e => e.editor === targetEditor)) {
	 	 	 	 skipped.push({ rowIndex: last.rowIndex, field: last.field });
	 	 	 }
	 	 	 return;
	 	 }

	 	 // Most recent entry IS targetEditor's 	 find the last entry by someone else
	 	 let i = entries.length - 1;
	 	 while (i >= 0 && entries[i].editor === targetEditor) i--;

	 	 const restoredValue = i >= 0 ? entries[i].newValue : entries[0].oldValue;
	 	 const col = last.field === "status" ? STATUS_COL : PARTIAL_COL;
	 	 const currentValue = sheet.getRange(last.rowIndex, col).getValue();

	 	 if (String(currentValue) === String(restoredValue)) return; // nothing to do

	 	 sheet.getRange(last.rowIndex, col).setValue(restoredValue);
	 	 logChange(sheet, last.rowIndex, last.field, currentValue, restoredValue, email, true);
	 	 reverted.push({ rowIndex: last.rowIndex, field: last.field, restoredTo: restoredValue });
	 });

	 return jsonResp({ ok: true, revertedCount: reverted.length, reverted, skippedCount: skipped.length, skipped });
}

// ?? History summary ????????????????????????????????????????????????????????
// Returns, for each editor who has ever made a change, how many (row,field)
// combinations currently have THEIR edit as the standing (most recent) value.
// Used by the admin panel to show "Jane Doe 	 12 standing changes" before
// deciding whether to purge them.
function handleHistory(body) {
	 const email = getVerifiedEmail(body);
	 if (!email) 	 	 	 	 	 	 	 return jsonResp({ ok: false, error: "Invalid or expired token" });
	 if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

	 const log = getChangelogSheet();
	 const allRows = log.getDataRange().getValues();
	 const groups = {};
	 for (let i = 1; i < allRows.length; i++) {
	 	 const r = allRows[i];
	 	 const key = r[1] + "|" + r[4];
	 	 if (!groups[key]) groups[key] = [];
	 	 groups[key].push({ timestamp: r[0], editor: String(r[7]).toLowerCase() });
	 }

	 const counts = {}; // editor -> standing change count
	 Object.keys(groups).forEach(key => {
	 	 const entries = groups[key];
	 	 entries.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
	 	 const last = entries[entries.length - 1];
	 	 counts[last.editor] = (counts[last.editor] || 0) + 1;
	 });

	 const summary = Object.keys(counts).map(e => ({ editor: e, standingChanges: counts[e] }))
	 	 .sort((a, b) => b.standingChanges - a.standingChanges);

	 return jsonResp({ ok: true, editors: summary });
}

// ── Pending-changes queue ─────────────────────────────────────────────────────
// Anyone with a verified Google sign-in may PROPOSE a status/partial_geometry
// change even if they're not on the Authorised sheet. It's written to the
// Pending sheet only — Data is untouched until an authorised editor reviews
// it via handlePendingReview. Keeps the low-friction "anyone can suggest a
// correction" property without giving write access to the canonical sheet.
function handlePropose(body) {
  const email = getVerifiedEmail(body);
  if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
  if (isBanned(email)) return jsonResp({ ok: false, error: "This account isn't permitted to submit changes." });
  if (isAuthorised(email)) return jsonResp({ ok: false, error: "You're already authorised — use the normal edit flow instead." });

  const row = parseInt(body.rowIndex, 10);
  const field = body.field;
  if (field !== "status" && field !== "partial_geometry") return jsonResp({ ok: false, error: "Invalid field" });

  let value;
  if (field === "status") {
    const allowed = ["Complete", "In_Progress", "Planned", "Not_Started"];
    value = body.newStatus;
    if (!allowed.includes(value)) return jsonResp({ ok: false, error: "Invalid status: " + value });
  } else {
    value = String(body.partialGeometry || "-");
    if (!/^[-|:.\d a-zA-Z]+$/.test(value)) return jsonResp({ ok: false, error: "Invalid partial geometry format" });
  }
  if (isNaN(row) || row < 2) return jsonResp({ ok: false, error: "Invalid row index" });

  const dataSheet = getDataSheet();
  if (row > dataSheet.getLastRow()) return jsonResp({ ok: false, error: "Row out of range" });

  const pending = getPendingSheet();
  const outstanding = countOutstandingPending(pending, email);
  if (outstanding >= MAX_PENDING_PER_SUBMITTER) {
    return jsonResp({ ok: false, error: "You already have " + outstanding + " submission(s) awaiting review — please wait for those before submitting more." });
  }

  const col = field === "status" ? STATUS_COL : PARTIAL_COL;
  const oldValue = dataSheet.getRange(row, col).getValue();
  const street = dataSheet.getRange(row, STREET_COL).getValue();
  const ward = dataSheet.getRange(row, WARD_COL).getValue();

  pending.appendRow([new Date().toISOString(), row, street, ward, field, oldValue, value, email, "Pending", "", ""]);
  return jsonResp({ ok: true });
}

// Returns every still-open (status === "Pending") submission for the admin
// review panel.
function handlePendingList(body) {
  const email = getVerifiedEmail(body);
  if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
  if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

  const sheet = getPendingSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return jsonResp({ ok: true, items: [] });

  const rows = sheet.getRange(2, 1, lastRow - 1, 11).getValues();
  const items = [];
  rows.forEach((r, i) => {
    if (r[8] !== "Pending") return;
    items.push({
      pendingRow: i + 2, timestamp: r[0], rowIndex: r[1], street: r[2], ward: r[3],
      field: r[4], oldValue: r[5], proposedValue: r[6], submitter: r[7]
    });
  });
  return jsonResp({ ok: true, items });
}

// Approve applies the proposed value to Data (via the normal changelog path,
// attributed to the original submitter — it's their edit, the reviewer just
// let it through) and marks the Pending row Approved. Deny just marks it
// Denied; Data is never touched. Either way the row stays for the audit
// trail rather than being deleted.
function handlePendingReview(body) {
  const email = getVerifiedEmail(body);
  if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
  if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

  const pendingRow = parseInt(body.pendingRow, 10);
  const decision = body.decision;
  if (decision !== "approve" && decision !== "deny") return jsonResp({ ok: false, error: "Invalid decision" });

  const sheet = getPendingSheet();
  if (isNaN(pendingRow) || pendingRow < 2 || pendingRow > sheet.getLastRow()) {
    return jsonResp({ ok: false, error: "Invalid pending row" });
  }

  const record = sheet.getRange(pendingRow, 1, 1, 11).getValues()[0];
  const rowIndex = record[1], field = record[4], proposedValue = record[6], submitter = record[7], status = record[8];
  if (status !== "Pending") return jsonResp({ ok: false, error: "Already reviewed" });

  if (decision === "approve") {
    const dataSheet = getDataSheet();
    if (rowIndex > dataSheet.getLastRow()) return jsonResp({ ok: false, error: "Row out of range" });
    const col = field === "status" ? STATUS_COL : PARTIAL_COL;
    const prevValue = dataSheet.getRange(rowIndex, col).getValue();
    dataSheet.getRange(rowIndex, col).setValue(proposedValue);
    // Re-read the SAME cell right back, in this same execution, before
    // marking anything reviewed. Apps Script reads/writes the live sheet
    // synchronously within one run, so this isn't "checking too soon" —
    // it's a genuine confirmation. If it doesn't match, leave the Pending
    // row untouched (still "Pending", so it can be retried) rather than
    // marking it Approved for a write that didn't actually land.
    const verifiedValue = dataSheet.getRange(rowIndex, col).getValue();
    if (String(verifiedValue) !== String(proposedValue)) {
      return jsonResp({ ok: false, error: `Wrote "${proposedValue}" to row ${rowIndex} but it now reads "${verifiedValue}" — not marked reviewed, please retry.` });
    }
    logChange(dataSheet, rowIndex, field, prevValue, proposedValue, submitter, false);
  }

  sheet.getRange(pendingRow, 9).setValue(decision === "approve" ? "Approved" : "Denied");
  sheet.getRange(pendingRow, 10).setValue(email);
  sheet.getRange(pendingRow, 11).setValue(new Date().toISOString());

  return jsonResp({ ok: true, decision, rowIndex, field, appliedValue: decision === "approve" ? proposedValue : null });
}

// Lets a browser check whether ITS OWN previously-submitted proposals are
// still outstanding, WITHOUT needing to be signed in as the submitter — this
// is what lets the "pending" preview clear itself once a reviewer resolves
// it (approve or deny), even if that browser has since signed out or
// switched to a different Google account. No token required: only reveals
// whether row+field has a Pending/Approved/Denied entry, never who
// submitted it or what value they proposed — no more than a road with a
// visible pending marker already implies.
function handlePendingStatus(body) {
  const rows = Array.isArray(body.rows) ? body.rows.slice(0, 200) : [];
  const statuses = {};
  if (!rows.length) return jsonResp({ ok: true, statuses });

  rows.forEach(r => { statuses[r.rowIndex + ":" + r.field] = "none"; });

  const sheet = getPendingSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const data = sheet.getRange(2, 1, lastRow - 1, 9).getValues();
    // Sheet rows are append-only in chronological order, so a plain
    // top-to-bottom overwrite naturally lands on the most recent entry per
    // row+field key — no explicit timestamp sort needed.
    const latest = {};
    data.forEach(r => { latest[r[1] + ":" + r[4]] = r[8]; });
    rows.forEach(r => {
      const key = r.rowIndex + ":" + r.field;
      if (key in latest) statuses[key] = latest[key]; // "Pending" | "Approved" | "Denied"
    });
  }

  return jsonResp({ ok: true, statuses });
}

// Banning stops future PROPOSE calls from this email — it does not touch
// anything they've already submitted (those stay in Pending for review as
// normal) or anything already approved into Data.
function handleBan(body) {
  const email = getVerifiedEmail(body);
  if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
  if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

  const target = String(body.targetEmail || "").toLowerCase().trim();
  if (!target) return jsonResp({ ok: false, error: "Missing targetEmail" });
  if (isAuthorised(target)) return jsonResp({ ok: false, error: "Can't ban an authorised editor — remove them from the Authorised sheet first if that's really intended." });

  if (!isBanned(target)) {
    getBannedSheet().appendRow([target, email, new Date().toISOString(), String(body.reason || "")]);
  }

  // A ban is almost always aimed at a bad-faith submitter — deny the rest of
  // their outstanding queue too, so the reviewer doesn't have to click
  // through the remainder one by one.
  const pending = getPendingSheet();
  const lastRow = pending.getLastRow();
  if (lastRow >= 2) {
    const rows = pending.getRange(2, 1, lastRow - 1, 9).getValues();
    rows.forEach((r, i) => {
      if (String(r[7]).toLowerCase() === target && r[8] === "Pending") {
        const pendingRow = i + 2;
        pending.getRange(pendingRow, 9).setValue("Denied");
        pending.getRange(pendingRow, 10).setValue(email);
        pending.getRange(pendingRow, 11).setValue(new Date().toISOString());
      }
    });
  }

  return jsonResp({ ok: true });
}

function handleUnban(body) {
  const email = getVerifiedEmail(body);
  if (!email) return jsonResp({ ok: false, error: "Invalid or expired token" });
  if (!isAuthorised(email)) return jsonResp({ ok: false, error: "Not authorised" });

  const target = String(body.targetEmail || "").toLowerCase().trim();
  if (!target) return jsonResp({ ok: false, error: "Missing targetEmail" });

  const sheet = getBannedSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const emails = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
    for (let i = 0; i < emails.length; i++) {
      if (String(emails[i][0]).trim().toLowerCase() === target) { sheet.deleteRow(i + 2); break; }
    }
  }
  return jsonResp({ ok: true });
}

function getChangelogSheet() {
	 const ss = SpreadsheetApp.getActiveSpreadsheet();
	 let sheet = ss.getSheetByName(CHANGELOG_SHEET);
	 if (!sheet) {
	 	 sheet = ss.insertSheet(CHANGELOG_SHEET);
	 	 sheet.appendRow(["timestamp", "rowIndex", "street", "ward", "field", "oldValue", "newValue", "editorEmail", "isRevert"]);
	 }
	 return sheet;
}

function logChange(dataSheet, row, field, oldValue, newValue, email, isRevert) {
	 const log = getChangelogSheet();
	 const street = dataSheet.getRange(row, STREET_COL).getValue();
	 const ward 	 = dataSheet.getRange(row, WARD_COL).getValue();
	 log.appendRow([
	 	 new Date().toISOString(),
	 	 row,
	 	 street,
	 	 ward,
	 	 field,
	 	 oldValue,
	 	 newValue,
	 	 email,
	 	 isRevert ? "TRUE" : "FALSE"
	 ]);
	 trimOldChangelogEntries(log);
}

// Deletes changelog rows older than CHANGELOG_MAX_AGE_DAYS.
// Runs on every write 	 cheap for a sheet of this size, no separate trigger needed.
function trimOldChangelogEntries(log) {
	 const lastRow = log.getLastRow();
	 if (lastRow < 2) return;

	 const cutoff = new Date();
	 cutoff.setDate(cutoff.getDate() - CHANGELOG_MAX_AGE_DAYS);

	 const timestamps = log.getRange(2, 1, lastRow - 1, 1).getValues();
	 const rowsToDelete = [];
	 for (let i = 0; i < timestamps.length; i++) {
	 	 const ts = new Date(timestamps[i][0]);
	 	 if (ts < cutoff) rowsToDelete.push(i + 2); // +2: 1-indexed, +1 for header row
	 }
	 // Delete from the bottom up so indices don't shift mid-deletion
	 rowsToDelete.sort((a, b) => b - a);
	 rowsToDelete.forEach(r => log.deleteRow(r));
}

// ?? Sheet helper ??????????????????????????????????????????????????????????????
// Returns the spreadsheet id and each key sheet's numeric gid, so a new
// deployment's index.html can be filled in (SHEET_GID/CHECKSUM_GID) without
// opening the Sheets UI — same pattern as Pothole Watch's sheetInfo action.
// No auth required: gids aren't sensitive, they're just needed to finish
// wiring up a fresh deployment's config.
function handleSheetInfo(body) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const dataSheet = getDataSheet();
  const checksumSheet = ss.getSheetByName("Checksum");
  return jsonResp({
    ok: true,
    spreadsheetId: ss.getId(),
    dataGid: dataSheet.getSheetId(),
    checksumGid: checksumSheet ? checksumSheet.getSheetId() : null
  });
}

function getDataSheet() {
	 const ss = SpreadsheetApp.getActiveSpreadsheet();
	 return ss.getSheetByName(DATA_SHEET_NAME) || ss.getSheets()[0];
}

function getPendingSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(PENDING_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(PENDING_SHEET);
    sheet.appendRow(["timestamp", "rowIndex", "street", "ward", "field", "oldValue", "proposedValue", "submitterEmail", "status", "reviewedBy", "reviewedAt"]);
  }
  return sheet;
}

function getBannedSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(BANNED_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(BANNED_SHEET);
    sheet.appendRow(["email", "bannedBy", "bannedAt", "reason"]);
  }
  return sheet;
}

function isBanned(email) {
  const sheet = getBannedSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return false;
  return sheet.getRange(2, 1, lastRow - 1, 1).getValues()
    .flat()
    .some(v => String(v).trim().toLowerCase() === email);
}

function countOutstandingPending(pendingSheet, email) {
  const lastRow = pendingSheet.getLastRow();
  if (lastRow < 2) return 0;
  return pendingSheet.getRange(2, 1, lastRow - 1, 9).getValues()
    .filter(r => String(r[7]).toLowerCase() === email && r[8] === "Pending")
    .length;
}

// ?? Auth helpers ??????????????????????????????????????????????????????????????
function getVerifiedEmail(body) {
  if (!GOOGLE_CLIENT_ID) {
    // Fails everyone rather than silently accepting mismatched tokens — but
    // throw instead of just returning null so whoever's setting up a new
    // deployment sees a clear reason instead of a generic "invalid token".
    throw new Error("Server misconfigured: set the GOOGLE_CLIENT_ID script property (Project Settings -> Script Properties) to this deployment's index.html GOOGLE_CLIENT_ID.");
  }
  try {
    if (body.idToken) {
      const res = UrlFetchApp.fetch(
        "https://oauth2.googleapis.com/tokeninfo?id_token=" + body.idToken,
        { muteHttpExceptions: true }
      );
      if (res.getResponseCode() !== 200) return null;
      const info = JSON.parse(res.getContentText());
      if (info.error || !info.email_verified) return null;
      // CRITICAL: without this aud check, any valid Google ID token —
      // including one minted for a completely different app — would be
      // accepted here as long as it belongs to an authorised person's
      // email. It must have been issued to THIS deployment's OAuth client.
      if (info.aud !== GOOGLE_CLIENT_ID) return null;
      return info.email.toLowerCase();
    }
    if (body.accessToken) {
      // tokeninfo?access_token= reports which client the token was actually
      // issued to (aud) — check that before trusting userinfo's response,
      // for the same reason as the idToken branch above.
      const tiRes = UrlFetchApp.fetch(
        "https://oauth2.googleapis.com/tokeninfo?access_token=" + body.accessToken,
        { muteHttpExceptions: true }
      );
      if (tiRes.getResponseCode() !== 200) return null;
      const tiInfo = JSON.parse(tiRes.getContentText());
      if (tiInfo.error || tiInfo.aud !== GOOGLE_CLIENT_ID) return null;

      const res = UrlFetchApp.fetch(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        { headers: { Authorization: "Bearer " + body.accessToken }, muteHttpExceptions: true }
      );
      if (res.getResponseCode() !== 200) return null;
      const info = JSON.parse(res.getContentText());
      return info.email ? info.email.toLowerCase() : null;
    }
    return null;
  } catch(e) { return null; }
}

function isAuthorised(email) {
	 if (DEMO_MODE) return true;
	 const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(AUTHORISED_SHEET);
	 if (!sheet) return false;
	 return sheet.getDataRange().getValues()
	 	 .flat()
	 	 .some(v => String(v).trim().toLowerCase() === email);
}

// ?? Response helper ???????????????????????????????????????????????????????????
function jsonResp(obj) {
	 return ContentService
	 	 .createTextOutput(JSON.stringify(obj))
	 	 .setMimeType(ContentService.MimeType.JSON);
}

