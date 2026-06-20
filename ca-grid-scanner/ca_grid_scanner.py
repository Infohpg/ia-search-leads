"""
ca_grid_scanner.py — CA Roof Scanner via geographic grid
No GIS API needed — sweeps lat/lng grid with satellite imagery.

Zone: South LA / Inglewood / Compton / Hawthorne / Gardena
      lat 33.850-33.990, lng -118.460 to -118.230 (~8,200 cells total)
Grid: ~200m spacing, zoom 19 (~381m×381m coverage per image)

Output: scan_results/leads_CA.csv + scan_results/revisar_CA.csv
Usage:
    OPENAI_API_KEY=... GOOGLE_MAPS_API_KEY=... python3 ca_grid_scanner.py
    DRY_RUN=1 ...  (no AI calls, just prints grid + exits)
    SCANNER_MAX=6000 ...  (default: scan 6000 cells per run)
"""

import os, csv, json, time, math, base64, io
import requests
from pathlib import Path
from datetime import datetime

# ─── CREDENCIALES ──────────────────────────────────────────────────────────────
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
MAPS_KEY   = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Infohpg/ia-search-leads")

if not OPENAI_KEY or not MAPS_KEY:
    raise RuntimeError("OPENAI_API_KEY y GOOGLE_MAPS_API_KEY son requeridas")

# ─── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
DRY_RUN        = os.environ.get("DRY_RUN", "0").strip() == "1"
STATE_TAG      = "CA"
MAX_AI_SPEND   = float(os.environ.get("MAX_AI_SPEND_USD", "5.00"))  # solo IA, Maps se paga aparte
MAX_CELLS      = int(os.environ.get("SCANNER_MAX", "6000"))

# Bounding box expandida: South LA + Inglewood + Compton + Hawthorne + Gardena
# Zona: stock residencial 1940s-1970s, alta densidad SFH
# Celdas disponibles: ~8,200 (200m step) → SCANNER_MAX=6000 corre las primeras 6,000
BBOX = {
    "lat_min": 33.850,
    "lat_max": 33.990,
    "lng_min": -118.460,
    "lng_max": -118.230,
}
GRID_STEP_M = 200  # metros entre centros de celda
ZOOM        = 19   # ~381m × 381m de cobertura por imagen

# Precios
IN_PRICE  = 0.15 / 1_000_000   # gpt-4o-mini entrada
OUT_PRICE = 0.60 / 1_000_000   # gpt-4o-mini salida
MAPS_SAT_PRICE   = 0.002        # Static Maps
GEOCODE_PRICE    = 0.005        # Geocoding API (solo para hits 8-10)

# ─── PATHS ─────────────────────────────────────────────────────────────────────
OUT_DIR     = Path("./scan_results"); OUT_DIR.mkdir(exist_ok=True)
LEADS_CSV   = OUT_DIR / f"leads_{STATE_TAG}.csv"
REVISAR_CSV = OUT_DIR / f"revisar_{STATE_TAG}.csv"
LOG_FILE    = OUT_DIR / "ca_scan.log"
CELL_CACHE  = OUT_DIR / "analyzed_cells_CA.json"

CSV_FIELDNAMES = [
    "Score", "Lona", "Dirección", "Año construcción", "Lat", "Lng",
    "Link Google Maps", "Tipo techo", "Descripción del daño",
    "Folio", "Estado",
    "Contactado (sí/no)", "Daño confirmado (sí/no/no visible)", "Notas del setter"
]

# ─── ESTADO GLOBAL ─────────────────────────────────────────────────────────────
STATS = {
    "cells_analyzed": 0,
    "ai_calls": 0,
    "geocode_calls": 0,
    "spend_ai": 0.0,
    "spend_maps": 0.0,
    "spend_geocode": 0.0,
    "hot_leads": 0,
    "revisar": 0,
    "errors": 0,
}

# ─── LOGGING ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ─── GRID GENERATION ───────────────────────────────────────────────────────────
def meters_to_deg_lat(m):
    return m / 111_320

def meters_to_deg_lng(m, lat):
    return m / (111_320 * math.cos(math.radians(lat)))

def generate_grid(bbox, step_m):
    """Genera lista de (lat, lng) centros de celda cubriendo el bbox."""
    step_lat = meters_to_deg_lat(step_m)
    step_lng = meters_to_deg_lng(step_m, (bbox["lat_min"] + bbox["lat_max"]) / 2)
    cells = []
    lat = bbox["lat_min"] + step_lat / 2
    while lat <= bbox["lat_max"]:
        lng = bbox["lng_min"] + step_lng / 2
        while lng <= bbox["lng_max"]:
            cells.append((round(lat, 7), round(lng, 7)))
            lng += step_lng
        lat += step_lat
    return cells

# ─── SATELLITE IMAGE ───────────────────────────────────────────────────────────
def get_sat_image(lat, lng, zoom=19, size="640x640"):
    url = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center":  f"{lat},{lng}",
        "zoom":    zoom,
        "size":    size,
        "maptype": "satellite",
        "key":     MAPS_KEY,
    }
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                STATS["spend_maps"] += MAPS_SAT_PRICE
                return base64.b64encode(r.content).decode()
        except Exception as e:
            if attempt == 2:
                return None
        time.sleep(1.5)
    return None

# ─── REVERSE GEOCODING ─────────────────────────────────────────────────────────
def reverse_geocode(lat, lng):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lng}", "key": MAPS_KEY, "result_type": "street_address"}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        STATS["geocode_calls"] += 1
        STATS["spend_geocode"] += GEOCODE_PRICE
        if data.get("results"):
            return data["results"][0].get("formatted_address", "")
    except Exception:
        pass
    return ""

# ─── AI ANALYSIS ───────────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """\
This is a satellite image of a residential neighborhood in Southern California.

Analyze ALL rooftops visible in this image and answer:

1. Are there any RESIDENTIAL rooftops (single-family homes) visible? (not commercial, warehouses, or large apartment complexes)
2. Do any of those roofs show:
   - Blue or silver TARP material draped over the surface
   - Visible HOLES, missing sections, or structural damage
   - Significant deterioration: heavy staining, missing tile areas, crumbling surface, exposed substrate

Score the WORST roof visible on a 1-10 scale:
- 8-10: CLEAR tarp or serious damage — obvious from satellite, high confidence
- 5-7: Possible deterioration or ambiguous material, could be damage
- 1-4: All roofs look intact, normal, no issues

Return ONLY valid JSON — no explanation, no markdown:
{"has_residential": true, "max_score": 3, "tarp_visible": false, "description": "All roofs appear intact, uniform tile pattern", "confidence": "high"}

"confidence" must be "high", "medium", or "low".
If no residential rooftops are visible, return max_score: 0."""

def analyze_cell(img_b64, lat, lng):
    """Envía imagen a GPT-mini para análisis. Retorna dict o None en error."""
    payload = {
        "model": "gpt-4o-mini",
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{img_b64}",
                    "detail": "high"
                }},
                {"type": "text", "text": ANALYSIS_PROMPT}
            ]
        }]
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers=headers, json=payload, timeout=30)
            d = r.json()
            usage = d.get("usage", {})
            cost = (usage.get("prompt_tokens", 0) * IN_PRICE +
                    usage.get("completion_tokens", 0) * OUT_PRICE)
            STATS["spend_ai"] += cost
            STATS["ai_calls"] += 1
            raw = d["choices"][0]["message"]["content"].strip()
            raw = raw.strip("` \n").lstrip("json").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt == 2:
                STATS["errors"] += 1
                return None
        time.sleep(2)
    return None

# ─── CSV WRITERS ───────────────────────────────────────────────────────────────
def _write_row(path, record):
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(record)

def save_lead(lat, lng, address, score, description, tarp, estado):
    gmaps = f"https://maps.google.com/?q={lat},{lng}"
    row = {
        "Score":                          score,
        "Lona":                           "SÍ" if tarp else "",
        "Dirección":                      address or gmaps,
        "Año construcción":               "",
        "Lat":                            lat,
        "Lng":                            lng,
        "Link Google Maps":               gmaps,
        "Tipo techo":                     "",
        "Descripción del daño":           description,
        "Folio":                          f"CA_{lat}_{lng}",
        "Estado":                         estado,
        "Contactado (sí/no)":             "",
        "Daño confirmado (sí/no/no visible)": "",
        "Notas del setter":               "",
    }
    if score >= 8:
        _write_row(LEADS_CSV, row)
        STATS["hot_leads"] += 1
    else:
        _write_row(REVISAR_CSV, row)
        STATS["revisar"] += 1

# ─── CELL CACHE ────────────────────────────────────────────────────────────────
def load_cache():
    if CELL_CACHE.exists():
        try:
            return set(json.loads(CELL_CACHE.read_text()))
        except Exception:
            return set()
    return set()

def save_cache(analyzed_cells):
    CELL_CACHE.write_text(json.dumps(list(analyzed_cells)))

# ─── GITHUB PUSH ───────────────────────────────────────────────────────────────
def push_to_github(local_path, gh_path):
    if not GITHUB_TOKEN or not local_path.exists():
        return
    try:
        hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}"
        existing = requests.get(api, headers=hdrs, timeout=15)
        sha = existing.json().get("sha", "") if existing.status_code == 200 else ""
        content = base64.b64encode(local_path.read_bytes()).decode()
        payload = {
            "message": f"auto: CA scan {datetime.now().strftime('%Y-%m-%d')}",
            "content": content,
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(api, headers=hdrs, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log(f"✅ GitHub push: {gh_path}")
        else:
            log(f"⚠️ GitHub push failed {gh_path}: {r.status_code}")
    except Exception as e:
        log(f"⚠️ GitHub error: {e}")

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log(f"{'='*60}")
    log(f"CA GRID SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log(f"Zona: Inglewood, CA — bbox {BBOX}")
    log(f"Grid: {GRID_STEP_M}m step, zoom {ZOOM} | DRY_RUN={DRY_RUN}")
    log(f"Límite IA: ${MAX_AI_SPEND:.2f} | MAX_CELLS={MAX_CELLS}")
    log(f"{'='*60}")

    cells = generate_grid(BBOX, GRID_STEP_M)
    log(f"Grid generado: {len(cells)} celdas")

    if DRY_RUN:
        log("DRY_RUN=1 — imprimiendo primeras 10 celdas y saliendo")
        for c in cells[:10]:
            log(f"  celda: lat={c[0]}, lng={c[1]} → {f'https://maps.google.com/?q={c[0]},{c[1]}'}")
        return

    analyzed_cells = load_cache()
    log(f"Cache: {len(analyzed_cells)} celdas ya analizadas")

    new_cells = [c for c in cells if f"{c[0]}_{c[1]}" not in analyzed_cells]
    log(f"Celdas nuevas a analizar: {len(new_cells)}")

    for i, (lat, lng) in enumerate(new_cells[:MAX_CELLS]):
        if STATS["spend_ai"] >= MAX_AI_SPEND:
            log(f"⛔ Límite IA alcanzado (${STATS['spend_ai']:.3f} >= ${MAX_AI_SPEND})")
            break

        cell_key = f"{lat}_{lng}"

        # Imagen satelital
        img = get_sat_image(lat, lng, zoom=ZOOM)
        if not img:
            STATS["errors"] += 1
            log(f"  ⚠️ Sin imagen para celda {i+1}: {lat},{lng}")
            time.sleep(2)
            continue

        # Análisis AI
        result = analyze_cell(img, lat, lng)
        analyzed_cells.add(cell_key)
        STATS["cells_analyzed"] += 1

        if not result or not result.get("has_residential"):
            # Celda sin casas residenciales — skip silencioso
            if i % 50 == 0:
                total_spend = STATS["spend_ai"] + STATS["spend_maps"]
                log(f"  [{i+1}/{len(new_cells)}] {lat},{lng} → no residencial | gasto: ${total_spend:.3f}")
            time.sleep(0.3)
            continue

        score = result.get("max_score", 0)
        tarp  = result.get("tarp_visible", False)
        desc  = result.get("description", "")
        conf  = result.get("confidence", "medium")

        if score >= 5:
            # Falso positivo obvio con baja confianza y score bajo → skip
            if score <= 5 and conf == "low":
                log(f"  [{i+1}] SKIP FP probable (score={score}, conf=low): {lat},{lng}")
                time.sleep(0.3)
                continue

            # Reverse geocode para hits relevantes
            address = ""
            if score >= 7:
                address = reverse_geocode(lat, lng)

            estado = "caliente" if score >= 8 else "revisar"
            save_lead(lat, lng, address, score, desc, tarp, estado)

            tag = "🔥 CALIENTE" if score >= 8 else "🟡 POSIBLE"
            log(f"  [{i+1}] {tag} score={score} tarp={tarp} | {address or f'{lat},{lng}'}")
        else:
            if i % 100 == 0:
                total_spend = STATS["spend_ai"] + STATS["spend_maps"]
                log(f"  [{i+1}/{len(new_cells)}] limpio (score={score}) | gasto: ${total_spend:.3f}")

        # Guardar cache periódicamente
        if i % 50 == 0:
            save_cache(analyzed_cells)

        time.sleep(0.4)

    save_cache(analyzed_cells)

    # Push a GitHub
    push_to_github(LEADS_CSV,   f"data/leads_{STATE_TAG}.csv")
    push_to_github(REVISAR_CSV, f"data/revisar_{STATE_TAG}.csv")

    # ── REPORTE FINAL ──
    total_spend = STATS["spend_ai"] + STATS["spend_maps"] + STATS["spend_geocode"]
    log("")
    log("=" * 60)
    log(f"REPORTE FINAL — CA Grid Scanner")
    total_spend = STATS["spend_ai"] + STATS["spend_maps"] + STATS["spend_geocode"]
    log(f"  Celdas analizadas:  {STATS['cells_analyzed']}")
    log(f"  Leads calientes:    {STATS['hot_leads']}  (score 8-10) → leads_CA.csv")
    log(f"  En revisión:        {STATS['revisar']}  (score 5-7) → revisar_CA.csv")
    log(f"  Errores:            {STATS['errors']}")
    log(f"  Costo IA:           ${STATS['spend_ai']:.4f}")
    log(f"  Costo Maps (sat):   ${STATS['spend_maps']:.4f}")
    log(f"  Costo Geocoding:    ${STATS['spend_geocode']:.4f}")
    log(f"  TOTAL estimado:     ${total_spend:.4f}")
    log("=" * 60)

if __name__ == "__main__":
    main()
