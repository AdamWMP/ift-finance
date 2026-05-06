/**
 * IFT Finance — Sales Board read/write web app.
 *
 *   GET  ?token=<SECRET>&period=S26   → returns rows + 2D grid for the tab
 *   POST {token, period, cell, value} → writes value into that cell
 *
 * After editing this file: Save (⌘S) then Deploy → New deployment → Web app.
 */

const SECRET = 'newminds123';

function doGet(e) {
  if ((e.parameter.token || '') !== SECRET) {
    return jsonResponse({ error: 'unauthorised' });
  }
  const period = (e.parameter.period || 'S26').trim();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(period);
  if (!sheet) return jsonResponse({ error: 'tab_not_found', period: period });

  // GET-style write fallback — POST + JSON body gets stripped on some
  // networks (Render's outbound proxy strips the body across the Apps
  // Script 302 redirect). Allowing writes via GET query params is the
  // robust workaround.
  if (e.parameter.action === 'write') {
    const cell = (e.parameter.cell || '').trim();
    const value = e.parameter.value;
    if (!cell || value === undefined) return jsonResponse({ error: 'missing_cell_or_value' });
    try {
      const num = Number(value);
      sheet.getRange(cell).setValue(isFinite(num) ? num : value);
    } catch (err) {
      return jsonResponse({ error: 'write_failed', detail: String(err) });
    }
    return jsonResponse({ ok: true, period: period, cell: cell, value: value });
  }

  const grid = sheet.getDataRange().getValues();
  if (!grid.length) return jsonResponse({ rows: [], grid: [] });

  const rawGrid = grid.map(row => row.map(v =>
    (v instanceof Date) ? v.toISOString().slice(0,10) : v
  ));

  const headers = grid[0].map(h => String(h).trim());
  const out = [];
  for (let i = 1; i < grid.length; i++) {
    const r = grid[i];
    const obj = {};
    let hasAny = false;
    for (let j = 0; j < headers.length; j++) {
      if (!headers[j]) continue;
      const v = r[j];
      if (v !== '' && v !== null && v !== undefined) hasAny = true;
      obj[headers[j]] = (v instanceof Date) ? v.toISOString().slice(0,10) : v;
    }
    if (hasAny) out.push(obj);
  }

  return jsonResponse({
    period: period,
    rows: out, count: out.length,
    grid: rawGrid, gridRows: rawGrid.length, gridCols: rawGrid[0]?.length || 0,
  });
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents || '{}'); }
  catch (err) { return jsonResponse({ error: 'bad_json' }); }

  if ((body.token || '') !== SECRET) return jsonResponse({ error: 'unauthorised' });

  const period = (body.period || 'S26').trim();
  const cell   = (body.cell || '').trim();
  const value  = body.value;
  if (!cell || value === undefined) return jsonResponse({ error: 'missing_cell_or_value' });

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(period);
  if (!sheet) return jsonResponse({ error: 'tab_not_found', period: period });

  try {
    sheet.getRange(cell).setValue(value);
  } catch (err) {
    return jsonResponse({ error: 'write_failed', detail: String(err) });
  }
  return jsonResponse({ ok: true, period: period, cell: cell, value: value });
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
