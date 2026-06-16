/**
 * HPG IA Search Leads — Apps Script v3
 * Sheet ID: 18OtBmciCs3knSW3gaStRbK9p1zku5ckq74AhSWtFIbE
 *
 * ═══════════════════════════════════════════════════════════════
 * PARA RESETEAR DE CERO (una sola vez después de pegar esto):
 *   1. Extensiones → Apps Script → pegá todo este archivo
 *   2. Guardá (Ctrl+S)
 *   3. En el menú de funciones seleccioná "nukeAndReimport"
 *   4. Ejecutá ▶ y autorizá
 *   → Borra triggers viejos, limpia hojas, reimporta limpio,
 *     configura trigger nuevo cada 6 horas. Todo en un paso.
 *
 * IMPORTAR MANUALMENTE:
 *   - Ejecutá importLeadsFromGitHub() desde el editor
 *   - O usá el menú "🏠 HPG Leads" que aparece en el Sheet
 * ═══════════════════════════════════════════════════════════════
 *
 * COMPORTAMIENTO:
 *   - Lee data/leads_para_ventas.csv desde GitHub (actualizado por el scanner)
 *   - Estado "confirmado" → hoja "Leads"    (para el equipo de ventas)
 *   - Estado "revisar"    → hoja "Revisar"  (verificación humana primero)
 *   - Dedup por folio: nunca duplica leads ya importados
 *   - Si un lead pasa de "revisar" a "confirmado" en el CSV, se mueve
 *
 * FIXES v3 sobre v2:
 *   - BOM UTF-8 eliminado antes de parsear (causaba fallos de lookup)
 *   - Columna Folio forzada a texto (@) para preservar ceros iniciales
 *     (Sheets convertía "0431060450220" → número 431060450220 → dedup fallaba)
 *   - _getExistingFolios normaliza números truncados (padding 13 dígitos)
 *   - Guard explícito si "Folio" o "Estado" no se encuentran en el CSV header
 *   - Trigger: 15 min → 6 horas
 *   - nukeAndReimport(): reset completo en una función
 */

// ── Configuración ────────────────────────────────────────────────────────────
const GITHUB_RAW_CSV  = "https://raw.githubusercontent.com/Infohpg/ia-search-leads/main/data/leads_para_ventas.csv";
const SPREADSHEET_ID  = "18OtBmciCs3knSW3gaStRbK9p1zku5ckq74AhSWtFIbE";
const TAB_CONFIRMADOS = "Leads";
const TAB_REVISAR     = "Revisar";

const SHEET_HEADERS = [
  "Score", "Lona", "Dirección", "Año construcción", "Lat", "Lng",
  "Link Google Maps", "Tipo techo", "Descripción del daño", "Folio",
  "Estado", "Contactado (sí/no)", "Daño confirmado (sí/no/no visible)",
  "Notas del setter", "Importado"
];

// Folio está en la posición 10 (1-indexed) — no cambiar si cambian headers
const FOLIO_SHEET_COL = SHEET_HEADERS.indexOf("Folio") + 1;  // = 10


// ── Reset completo ───────────────────────────────────────────────────────────
/**
 * Borra todos los triggers viejos, limpia ambas hojas, reimporta desde cero
 * y configura el trigger nuevo a cada 6 horas.
 * Ejecutar UNA SOLA VEZ después de pegar este script.
 */
function nukeAndReimport() {
  // 1. Matar todos los triggers de importLeadsFromGitHub
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === "importLeadsFromGitHub") {
      ScriptApp.deleteTrigger(t);
    }
  });
  Logger.log("Triggers eliminados.");

  // 2. Limpiar hojas
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  [TAB_CONFIRMADOS, TAB_REVISAR].forEach(name => {
    let sheet = ss.getSheetByName(name);
    if (sheet) {
      sheet.clearContents();
      sheet.clearFormats();
    } else {
      sheet = ss.insertSheet(name);
    }
    sheet.appendRow(SHEET_HEADERS);
    _applyHeaderFormat(sheet);
    _setFolioAsText(sheet);  // CRÍTICO: antes de escribir datos
  });
  Logger.log("Hojas limpiadas y preparadas.");

  // 3. Reimportar limpio
  importLeadsFromGitHub();

  // 4. Trigger nuevo: cada 6 horas
  ScriptApp.newTrigger("importLeadsFromGitHub")
    .timeBased()
    .everyHours(6)
    .create();

  Logger.log("✅ nukeAndReimport completo. Trigger: cada 6 horas.");
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "✅ Reset completo. Trigger cada 6h activo.", "HPG IA Leads", 10
    );
  } catch(e) {}
}


// ── Función principal de importación ────────────────────────────────────────
function importLeadsFromGitHub() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const tabConf   = _getOrCreateSheet(ss, TAB_CONFIRMADOS);
  const tabReview = _getOrCreateSheet(ss, TAB_REVISAR);

  // Cargar folios ya existentes en cada hoja (dedup)
  const foliosConf   = _getExistingFolios(tabConf);
  const foliosReview = _getExistingFolios(tabReview);

  // Fetch CSV de GitHub
  const response = UrlFetchApp.fetch(GITHUB_RAW_CSV, { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) {
    Logger.log("ERROR al obtener CSV: HTTP " + response.getResponseCode());
    return;
  }

  // Eliminar BOM (﻿) si existe — causaba que colIdx["Folio"] fallara
  const csvText = response.getContentText("UTF-8").replace(/^﻿/, "");
  const rows = Utilities.parseCsv(csvText);
  if (!rows || rows.length < 2) { Logger.log("CSV vacío o sin datos."); return; }

  const csvHeader = rows[0];
  const colIdx    = _buildColIndex(csvHeader);
  const folioCol  = colIdx["Folio"];
  const estadoCol = colIdx["Estado"];

  // Guard: si los headers no se encuentran, el CSV cambió de formato
  if (folioCol === undefined || estadoCol === undefined) {
    Logger.log("ERROR CRÍTICO: columnas 'Folio' o 'Estado' no encontradas en CSV. Headers: " + csvHeader.join(" | "));
    return;
  }

  const tsNow = new Date().toLocaleString("es-MX", { timeZone: "America/New_York" });
  let addedConf = 0, addedReview = 0, skipped = 0, moved = 0;

  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || row.length === 0) continue;

    const folio  = String(row[folioCol]  || "").trim();
    const estado = String(row[estadoCol] || "").trim().toLowerCase();
    if (!folio) continue;

    const sheetRow = _buildSheetRow(csvHeader, row, colIdx, tsNow);

    if (estado === "confirmado") {
      if (foliosConf.has(folio)) {
        // Ya en Leads — ¿también en Revisar? → mover
        if (foliosReview.has(folio)) {
          _removeRowByFolio(tabReview, folio);
          foliosReview.delete(folio);
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

    } else {
      // Estado desconocido — loguear sin agregar
      Logger.log("Estado desconocido para folio " + folio + ": '" + estado + "'");
    }
  }

  _applyFormatting(tabConf);
  _applyFormatting(tabReview, true);

  const summary = `✅ +${addedConf} confirmados | +${addedReview} revisar | ${skipped} ya existían | ${moved} movidos | ${tsNow}`;
  Logger.log(summary);
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast(summary, "HPG IA Leads", 8);
  } catch(e) {}
}


// ── Helpers ──────────────────────────────────────────────────────────────────

function _getOrCreateSheet(ss, name) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(SHEET_HEADERS);
    _applyHeaderFormat(sheet);
    _setFolioAsText(sheet);
  } else if (sheet.getLastRow() === 0) {
    sheet.appendRow(SHEET_HEADERS);
    _applyHeaderFormat(sheet);
    _setFolioAsText(sheet);
  }
  return sheet;
}

/**
 * Formatea la columna Folio como texto puro.
 * DEBE llamarse ANTES de escribir datos — evita que Sheets convierta
 * "0431060450220" en el número 431060450220 (pierde el cero inicial).
 */
function _setFolioAsText(sheet) {
  sheet.getRange(2, FOLIO_SHEET_COL, 1000, 1).setNumberFormat("@");
}

/**
 * Lee los folios ya importados en una hoja.
 * Maneja el caso donde Sheets convirtió el folio a número (perdiendo ceros):
 * si el valor tiene 10-12 dígitos, agrega también la versión con 13 dígitos.
 */
function _getExistingFolios(sheet) {
  const folios = new Set();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return folios;

  const values = sheet.getRange(2, FOLIO_SHEET_COL, lastRow - 1, 1).getValues();
  values.forEach(r => {
    if (r[0] === "" || r[0] === null || r[0] === undefined) return;
    const raw = String(r[0]).trim();
    folios.add(raw);
    // Normalización: Sheets puede haber convertido "0431060450220" → 431060450220
    // Si el valor tiene 10-12 dígitos numéricos, agregar también la versión con 13
    if (/^\d{10,12}$/.test(raw)) {
      folios.add(raw.padStart(13, "0"));
    }
  });
  return folios;
}

function _buildColIndex(header) {
  const idx = {};
  header.forEach((col, i) => { idx[col.trim()] = i; });
  return idx;
}

function _buildSheetRow(csvHeader, csvRow, colIdx, ts) {
  return SHEET_HEADERS.map(h => {
    if (h === "Importado") return ts;
    const ci = colIdx[h];
    // Forzar string para evitar conversión numérica en appendRow
    return ci !== undefined ? String(csvRow[ci] || "") : "";
  });
}

function _removeRowByFolio(sheet, folio) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  const values = sheet.getRange(2, FOLIO_SHEET_COL, lastRow - 1, 1).getValues();
  for (let i = values.length - 1; i >= 0; i--) {
    if (String(values[i][0]).trim() === folio) {
      sheet.deleteRow(i + 2);
      return;
    }
  }
}

function _applyHeaderFormat(sheet) {
  const r = sheet.getRange(1, 1, 1, SHEET_HEADERS.length);
  r.setBackground("#1a73e8").setFontColor("#ffffff").setFontWeight("bold");
  sheet.setFrozenRows(1);
}

function _applyFormatting(sheet, isRevisar) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
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
    rules.push(
      SpreadsheetApp.newConditionalFormatRule()
        .whenTextContains("revisar")
        .setBackground("#ffe8cc").build()
    );
  }
  sheet.setConditionalFormatRules(rules);
  sheet.autoResizeColumns(1, SHEET_HEADERS.length);
}


// ── Trigger setup ────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === "importLeadsFromGitHub") {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger("importLeadsFromGitHub")
    .timeBased()
    .everyHours(6)
    .create();
  Logger.log("✅ Trigger configurado: cada 6 horas");
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast("Trigger cada 6h configurado ✅", "Setup", 5);
  } catch(e) {}
}


// ── Menú en el Sheet ─────────────────────────────────────────────────────────
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("🏠 HPG Leads")
    .addItem("📥 Importar ahora",            "importLeadsFromGitHub")
    .addItem("🔥 Reset completo + reimport",  "nukeAndReimport")
    .addSeparator()
    .addItem("⚙️ Configurar trigger 6h",     "setupTrigger")
    .addToUi();
}
