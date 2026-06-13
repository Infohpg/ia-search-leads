/**
 * HPG IA Search Leads — Apps Script v2
 * Sheet ID: 18OtBmciCs3knSW3gaStRbK9p1zku5ckq74AhSWtFIbE
 *
 * ═══════════════════════════════════════════════════════════════
 * INSTALACIÓN (hacer UNA SOLA VEZ):
 * 1. Abrí el Sheet: https://docs.google.com/spreadsheets/d/18OtBmciCs3knSW3gaStRbK9p1zku5ckq74AhSWtFIbE
 * 2. Extensiones → Apps Script
 * 3. Borrá todo el contenido de Code.gs y pegá ESTE archivo completo
 * 4. Guardá (Ctrl+S / Cmd+S)
 * 5. En el menú desplegable de funciones, seleccioná "setupTrigger"
 * 6. Hacé clic en ▶ Ejecutar
 * 7. Autorizá cuando Google lo pida (permisos de Google Sheets + URL fetch)
 * → Eso crea el trigger de importación cada 15 minutos
 *
 * IMPORTAR MANUALMENTE:
 * - Ejecutá importLeadsFromGitHub() desde el editor, o
 * - Usá el menú "🏠 HPG Leads" que aparece en el Sheet
 * ═══════════════════════════════════════════════════════════════
 *
 * COMPORTAMIENTO:
 * - Lee data/leads_para_ventas.csv desde GitHub raw (actualizado por el scanner diariamente)
 * - Estado "confirmado" → hoja "Leads" (para el equipo de ventas)
 * - Estado "revisar"    → hoja "Revisar" (requiere verificación antes de contactar)
 * - Dedup por folio: no duplica leads ya importados en sesiones anteriores
 * - Si un lead "revisar" pasa a "confirmado" en el CSV, se mueve automáticamente
 */

// ── Configuración ───────────────────────────────────────────────────────────
const GITHUB_RAW_CSV   = "https://raw.githubusercontent.com/Infohpg/ia-search-leads/main/data/leads_para_ventas.csv";
const GITHUB_RAW_HIST  = "https://raw.githubusercontent.com/Infohpg/ia-search-leads/main/data/run_history.csv";
const SPREADSHEET_ID   = "18OtBmciCs3knSW3gaStRbK9p1zku5ckq74AhSWtFIbE";
const TAB_CONFIRMADOS  = "Leads";
const TAB_REVISAR      = "Revisar";
const FOLIO_COL_IDX    = 10;  // columna "Folio" (1-indexed) en el CSV — posición 10

// ── Headers que se escriben en el Sheet ────────────────────────────────────
// Coinciden con CSV_FIELDNAMES del scanner más columna "Importado"
const SHEET_HEADERS = [
  "Score", "Lona", "Dirección", "Año construcción", "Lat", "Lng",
  "Link Google Maps", "Tipo techo", "Descripción del daño", "Folio",
  "Estado", "Contactado (sí/no)", "Daño confirmado (sí/no/no visible)",
  "Notas del setter", "Importado"
];


// ── Función principal ───────────────────────────────────────────────────────
function importLeadsFromGitHub() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);

  // Obtener o crear hojas
  const tabConf   = _getOrCreateSheet(ss, TAB_CONFIRMADOS);
  const tabReview = _getOrCreateSheet(ss, TAB_REVISAR);

  // Cargar folios ya importados (dedup)
  const foliosConf   = _getExistingFolios(tabConf);
  const foliosReview = _getExistingFolios(tabReview);

  // Fetch CSV de GitHub
  const response = UrlFetchApp.fetch(GITHUB_RAW_CSV, { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) {
    const msg = `ERROR al obtener CSV: HTTP ${response.getResponseCode()}`;
    Logger.log(msg);
    _showAlert(msg);
    return;
  }

  const rows = Utilities.parseCsv(response.getContentText("UTF-8"));
  if (!rows || rows.length < 2) {
    Logger.log("CSV vacío o sin datos");
    return;
  }

  // Mapear índices de columnas desde el header del CSV
  const csvHeader  = rows[0];
  const colIdx     = _buildColIndex(csvHeader);
  const folioCol   = colIdx["Folio"];
  const estadoCol  = colIdx["Estado"];
  const tsNow      = new Date().toLocaleString("es-MX", { timeZone: "America/New_York" });

  let addedConf = 0, addedReview = 0, skipped = 0, moved = 0;

  for (let i = 1; i < rows.length; i++) {
    const row    = rows[i];
    if (!row || row.length === 0) continue;

    const folio  = (row[folioCol] || "").trim();
    const estado = (row[estadoCol] || "confirmado").trim().toLowerCase();
    if (!folio) continue;

    // Build sheet row: all CSV cols + "Importado" timestamp
    const sheetRow = _buildSheetRow(csvHeader, row, colIdx, tsNow);

    if (estado === "confirmado") {
      if (foliosConf.has(folio)) {
        // Ya existe en Leads — verificar si vino de Revisar (movido)
        if (foliosReview.has(folio)) {
          _removeRowByFolio(tabReview, folio, folioCol);
          moved++;
        }
        skipped++;
        continue;
      }
      tabConf.appendRow(sheetRow);
      foliosConf.add(folio);
      addedConf++;
    } else if (estado === "revisar") {
      if (foliosReview.has(folio) || foliosConf.has(folio)) {
        skipped++;
        continue;
      }
      tabReview.appendRow(sheetRow);
      foliosReview.add(folio);
      addedReview++;
    }
  }

  // Aplicar formato a ambas hojas
  _applyFormatting(tabConf);
  _applyFormatting(tabReview, true);

  const summary = `✅ Import: +${addedConf} confirmados | +${addedReview} revisar | ${skipped} ya existían | ${moved} movidos | ${tsNow}`;
  Logger.log(summary);
  _showMenuAlert(ss, summary);
}


// ── Helpers ─────────────────────────────────────────────────────────────────

function _getOrCreateSheet(ss, name) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(SHEET_HEADERS);
    _applyHeaderFormat(sheet);
  } else if (sheet.getLastRow() === 0) {
    sheet.appendRow(SHEET_HEADERS);
    _applyHeaderFormat(sheet);
  }
  return sheet;
}

function _getExistingFolios(sheet) {
  const folios = new Set();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return folios;

  // Folio está en columna SHEET_HEADERS posición 10 (1-indexed = col 10)
  const folioSheetCol = SHEET_HEADERS.indexOf("Folio") + 1;
  if (folioSheetCol === 0) return folios;

  const values = sheet.getRange(2, folioSheetCol, lastRow - 1, 1).getValues();
  values.forEach(r => { if (r[0]) folios.add(String(r[0]).trim()); });
  return folios;
}

function _buildColIndex(header) {
  const idx = {};
  header.forEach((col, i) => { idx[col.trim()] = i; });
  return idx;
}

function _buildSheetRow(csvHeader, csvRow, colIdx, ts) {
  // Map each SHEET_HEADER to corresponding CSV value (or "" if not found)
  return SHEET_HEADERS.map(h => {
    if (h === "Importado") return ts;
    const ci = colIdx[h];
    return ci !== undefined ? (csvRow[ci] || "") : "";
  });
}

function _removeRowByFolio(sheet, folio, folioColIdx) {
  const folioSheetCol = SHEET_HEADERS.indexOf("Folio") + 1;
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  const values = sheet.getRange(2, folioSheetCol, lastRow - 1, 1).getValues();
  for (let i = values.length - 1; i >= 0; i--) {
    if (String(values[i][0]).trim() === folio) {
      sheet.deleteRow(i + 2);
      return;
    }
  }
}

function _applyHeaderFormat(sheet) {
  const headerRange = sheet.getRange(1, 1, 1, SHEET_HEADERS.length);
  headerRange.setBackground("#1a73e8").setFontColor("#ffffff").setFontWeight("bold");
  sheet.setFrozenRows(1);
}

function _applyFormatting(sheet, isRevisar) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  // Score column conditional formatting (col 1)
  const scoreRange = sheet.getRange(2, 1, lastRow - 1, 1);
  const rules = [];
  if (!isRevisar) {
    rules.push(
      SpreadsheetApp.newConditionalFormatRule()
        .whenNumberGreaterThanOrEqualTo(9)
        .setBackground("#d4edda").build(),
      SpreadsheetApp.newConditionalFormatRule()
        .whenNumberBetween(7, 8)
        .setBackground("#fff3cd").build()
    );
  } else {
    // Revisar tab: highlight everything yellow-orange
    rules.push(
      SpreadsheetApp.newConditionalFormatRule()
        .whenTextContains("revisar")
        .setBackground("#ffe8cc").build()
    );
  }
  sheet.setConditionalFormatRules(rules);
  sheet.autoResizeColumns(1, SHEET_HEADERS.length);
}

function _showAlert(msg) {
  try {
    SpreadsheetApp.getUi().alert(msg);
  } catch(e) {
    Logger.log("(no UI disponible para mostrar alerta)");
  }
}

function _showMenuAlert(ss, msg) {
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast(msg, "HPG IA Leads", 8);
  } catch(e) {
    Logger.log(msg);
  }
}


// ── Trigger setup ───────────────────────────────────────────────────────────
function setupTrigger() {
  // Eliminar triggers existentes para esta función
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === "importLeadsFromGitHub") {
      ScriptApp.deleteTrigger(t);
    }
  });

  // Trigger cada 15 minutos — Apps Script lo desfasa automáticamente para no pegar todos a la vez
  ScriptApp.newTrigger("importLeadsFromGitHub")
    .timeBased()
    .everyMinutes(15)
    .create();

  Logger.log("✅ Trigger configurado: cada 15 minutos");
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast("Trigger cada 15 min configurado ✅", "Setup", 5);
  } catch(e) {}
}


// ── Menú en el Sheet ────────────────────────────────────────────────────────
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("🏠 HPG Leads")
    .addItem("📥 Importar leads ahora",        "importLeadsFromGitHub")
    .addSeparator()
    .addItem("⚙️ Configurar trigger diario",   "setupTrigger")
    .addToUi();
}
