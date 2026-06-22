"""
ca_gis_scanner.py — CA Roof Scanner usando GIS de LA County (casa por casa)
Mismo pipeline que FL: 1 casa = 1 foto satélite zoom 21 → STEP1 → FP_CHECK → STEP2 → scoring

Fuente de datos: LA County GIS Address Points API
  https://services.arcgis.com/RmCCgQtiZLDCtblq/arcgis/rest/services/
  eGIS_Addressing_ADDRESS_POINTSv2/FeatureServer/0/query

Uso:
  OPENAI_API_KEY=... GOOGLE_MAPS_API_KEY=... python3 ca_gis_scanner.py
  DRY_RUN=1 ...       (no llama APIs, muestra primeras 5 casas del GIS)
  MAX_ANALYZED=50 ... (test de 50 casas)
  SCANNER_BBOX=-118.385,33.940,-118.325,33.975   (Inglewood bbox)
"""

import os, csv, json, time, base64, random
import requests
from pathlib import Path
from datetime import datetime

# ─── CREDENCIALES ──────────────────────────────────────────────────────────────
# Prefiere OpenRouter (key válida). Fallback a OPENAI_API_KEY si OR no está.
_OR_KEY      = os.environ.get("OPENROUTER_API_KEY", "")
_OAI_KEY     = os.environ.get("OPENAI_API_KEY", "")
MAPS_KEY     = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Infohpg/ia-search-leads")

if _OR_KEY:
    AI_BASE    = "https://openrouter.ai/api/v1/chat/completions"
    AI_KEY     = _OR_KEY
    AI_MODEL   = "openai/gpt-4o-mini"
elif _OAI_KEY:
    AI_BASE    = "https://api.openai.com/v1/chat/completions"
    AI_KEY     = _OAI_KEY
    AI_MODEL   = "gpt-4o-mini"
else:
    raise RuntimeError("Se requiere OPENROUTER_API_KEY o OPENAI_API_KEY")

if not MAPS_KEY:
    raise RuntimeError("GOOGLE_MAPS_API_KEY es requerida")

# ─── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
DRY_RUN          = os.environ.get("DRY_RUN", "0").strip() == "1"
MAX_ANALYZED     = int(os.environ.get("MAX_ANALYZED", "6000"))
MAX_AI_SPEND     = float(os.environ.get("MAX_AI_SPEND_USD", "5.00"))
# Cap de registros a descargar del GIS por run. 30000 = ~3 min descarga, pool para 4-5 días.
# El bbox total tiene 257k+ propiedades — descargar todo toma 31 min y agota el DNS.
GIS_MAX_RECORDS  = int(os.environ.get("GIS_MAX_RECORDS", "30000"))
STATE_TAG        = "CA"

# Bbox predeterminada: Inglewood completo + buffer
# Override: SCANNER_BBOX=xmin,ymin,xmax,ymax  (lng_min,lat_min,lng_max,lat_max)
_raw_bbox = os.environ.get("SCANNER_BBOX", "-118.500,33.850,-118.100,34.050")
_b = [float(x) for x in _raw_bbox.split(",")]
BBOX = {"xmin": _b[0], "ymin": _b[1], "xmax": _b[2], "ymax": _b[3]}

# Precios
IN_PRICE  = 0.15 / 1_000_000
OUT_PRICE = 0.60 / 1_000_000
MAPS_SAT_PRICE  = 0.002
MAPS_SV_PRICE   = 0.007

# ─── PATHS ─────────────────────────────────────────────────────────────────────
OUT_DIR      = Path("./scan_results"); OUT_DIR.mkdir(exist_ok=True)
LEADS_CSV    = OUT_DIR / f"leads_{STATE_TAG}.csv"
REVISAR_CSV  = OUT_DIR / f"revisar_{STATE_TAG}.csv"
LOG_FILE     = OUT_DIR / "ca_gis_scan.log"
AIN_CACHE_F  = OUT_DIR / "analyzed_ain_CA.json"

CSV_FIELDNAMES = [
    "Score", "Lona", "Dirección", "Año construcción", "Lat", "Lng",
    "Link Google Maps", "Tipo techo", "Descripción del daño",
    "Folio", "Estado",
    "Contactado (sí/no)", "Daño confirmado (sí/no/no visible)", "Notas del setter"
]

# ─── STATS ─────────────────────────────────────────────────────────────────────
STATS = {
    "analyzed": 0, "hot": 0, "posible": 0, "clean": 0, "errors": 0,
    "step1_usd": 0.0, "step2_usd": 0.0, "maps_usd": 0.0,
}

# ─── LOGGING ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def logp(msg):
    print(msg, end="", flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg)

# ─── GIS LA COUNTY ─────────────────────────────────────────────────────────────
GIS_URL = ("https://services.arcgis.com/RmCCgQtiZLDCtblq/arcgis/rest/services/"
           "eGIS_Addressing_ADDRESS_POINTSv2/FeatureServer/0/query")

def fetch_properties_la(bbox, max_records=None):
    """Descarga propiedades del GIS de LA County, 1000 por página. max_records limita el total."""
    props = []
    seen_ain = set()
    offset = 0
    page = 0
    while True:
        params = {
            "where":           "1=1",
            "geometry":        f"{bbox['xmin']},{bbox['ymin']},{bbox['xmax']},{bbox['ymax']}",
            "geometryType":    "esriGeometryEnvelope",
            "inSR":            "4326",
            "spatialRel":      "esriSpatialRelIntersects",
            "outFields":       "FullAddress,AIN,ZipCode,PostComm1",
            "returnGeometry":  "true",
            "outSR":           "4326",
            "resultOffset":    offset,
            "resultRecordCount": 1000,
            "f":               "json",
        }
        for attempt in range(3):
            try:
                r = requests.get(GIS_URL, params=params, timeout=30)
                data = r.json()
                break
            except Exception as e:
                if attempt == 2:
                    log(f"  ⚠️ GIS error offset {offset}: {e}")
                    return props
                time.sleep(3)

        feats = data.get("features", [])
        page += 1
        if not feats:
            break

        for feat in feats:
            a   = feat.get("attributes", {})
            geo = feat.get("geometry", {})
            lat, lng = geo.get("y"), geo.get("x")
            if not (lat and lng):
                continue
            ain = str(a.get("AIN", "")).strip()
            if not ain or ain in seen_ain:
                continue
            if not (-119.0 < lng < -117.0) or not (33.0 < lat < 35.0):
                continue
            seen_ain.add(ain)
            full_addr = a.get("FullAddress", "").strip()
            city      = (a.get("PostComm1") or "").strip().title()
            zipcode   = str(a.get("ZipCode", "")).strip().replace(".0", "")
            address   = f"{full_addr}, {city}, CA {zipcode}".strip(", ")
            props.append({"address": address, "lat": lat, "lng": lng,
                          "ain": ain, "zip": zipcode})

        log(f"  GIS página {page} (offset {offset}): {len(feats)} features | acumulado: {len(props)}")

        # Fin: menos de 1000 resultados O flag explícito de que no hay más O cap alcanzado
        if len(feats) < 1000 or not data.get("exceededTransferLimit", False):
            break
        if max_records and len(props) >= max_records:
            log(f"  GIS cap alcanzado: {len(props)} >= {max_records}")
            break
        offset += 1000
        time.sleep(0.5)

    log(f"GIS descarga completa: {page} páginas, {len(props)} propiedades únicas")
    random.shuffle(props)
    return props

# ─── AIN CACHE ─────────────────────────────────────────────────────────────────
def load_ain_cache():
    if AIN_CACHE_F.exists():
        try: return set(json.loads(AIN_CACHE_F.read_text()))
        except: pass
    return set()

def save_ain_cache(cache):
    AIN_CACHE_F.write_text(json.dumps(list(cache)))

# ─── GOOGLE MAPS ───────────────────────────────────────────────────────────────
def _get_img(url, params):
    try:
        r = requests.get(url, params=params, timeout=18)
        if r.status_code == 200 and len(r.content) > 4000:
            return base64.b64encode(r.content).decode()
    except: pass
    return None

def satellite(lat, lng, zoom=21):
    img = _get_img("https://maps.googleapis.com/maps/api/staticmap",
        {"center": f"{lat},{lng}", "zoom": zoom, "size": "512x512",
         "maptype": "satellite", "key": MAPS_KEY})
    if img:
        STATS["maps_usd"] += MAPS_SAT_PRICE
    return img

def streetview(lat, lng, heading=0, fov=90):
    img = _get_img("https://maps.googleapis.com/maps/api/streetview",
        {"location": f"{lat},{lng}", "size": "512x512",
         "heading": heading, "fov": fov, "pitch": 10, "key": MAPS_KEY})
    if img:
        STATS["maps_usd"] += MAPS_SV_PRICE
    return img

def sv_available(lat, lng):
    """Chequea si hay Street View disponible. Retorna (bool, date_str)."""
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": f"{lat},{lng}", "key": MAPS_KEY}, timeout=10)
        d = r.json()
        if d.get("status") == "OK":
            return True, d.get("date", "")
    except: pass
    return False, ""

# ─── AI CALL ───────────────────────────────────────────────────────────────────
def ai_call(images, prompt, max_tokens=200, detail="low", bucket="step1"):
    """Llama a GPT-4o-mini con imágenes. Retorna dict o {"error": ...}."""
    msgs = [{"role": "user", "content":
        [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": detail}}
         for img in images if img]
        + [{"type": "text", "text": prompt}]
    }]
    for attempt in range(3):
        try:
            r = requests.post(AI_BASE,
                headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": max_tokens, "messages": msgs},
                timeout=30)
            d = r.json()
            usage = d.get("usage", {})
            cost = (usage.get("prompt_tokens", 0) * IN_PRICE +
                    usage.get("completion_tokens", 0) * OUT_PRICE)
            STATS[f"{bucket}_usd"] = STATS.get(f"{bucket}_usd", 0.0) + cost
            raw = d["choices"][0]["message"]["content"].strip()
            raw = raw.strip("` \n").lstrip("json").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt == 2:
                return {"error": str(e)}
            time.sleep(2)
    return {"error": "max_retries"}

# ─── PROMPTS (idénticos a FL — mismo pipeline) ─────────────────────────────────
STEP1_PROMPT = """Aerial satellite image. Examine the CENTER house (the building most centered in the image).

DETECTION 1 — TARP: Does the CENTER house ROOF have any covering material ON it?
- Any color: blue, navy, dark navy, silver, metallic, green, black, gray, tan, ANY non-natural color
- Old/weathered tarps look DARK NAVY or almost black — count these as YES
- Any material that looks like it was PLACED on the roof (not part of the original structure)
→ Set blue_or_silver_on_roof = true if YES

DETECTION 2 — HOLES/MISSING: Visible black gaps, holes, or missing tiles/shingles on the roof?
→ Set visible_holes_or_missing_sections = true if YES

DETECTION 3 — DETERIORATION: Any visible sign of wear, aging, or damage WITHOUT a tarp?
- Irregular tile pattern: displaced or broken tiles disrupting the uniform roof surface
- Strong discoloration or staining: large dark patches, black/green algae areas, rust stains
- Visible patchwork: sections of clearly DIFFERENT color or material (previous repairs)
- Worn or aged surface: inconsistent texture, crumbling material, exposed substrate or underlayment
→ Set visible_deterioration = true for ANY of the above — ALTO RECALL.
   Only skip if the whole roof looks uniformly sound.

BIAS RULE: If unsure whether something might be a tarp or damage — mark as TRUE. Better to
send a false positive to STEP2 than to miss a real damaged roof.

FALSE POSITIVE CHECK — only if blue_or_silver_on_roof is true:
  POOL: oval/rectangular blue water in the YARD at ground level? → obvious_false_positive = true
  PAINTED ROOF: whole roof is one solid permanent color with straight regular seams? → obvious_false_positive = true
  STRIPED TENT: colored stripes (blue+red+yellow) covering walls AND roof? → obvious_false_positive = true
  NEIGHBOR: material is on a building LEFT, RIGHT, or CORNER — NOT the centered house? → obvious_false_positive = true

Respond ONLY with valid JSON (no markdown):
{"blue_or_silver_on_roof":false,"visible_holes_or_missing_sections":false,"obvious_false_positive":false,"visible_deterioration":false}"""

FP_CHECK_PROMPT = """Aerial satellite image. Blue or silver material was detected on or near the CENTER house.
Is this image clearly one of these three specific false positives?
  1. FUMIGATION TENT: entire house wrapped in striped tent (blue+red/yellow visible on walls/sides).
  2. NEIGHBOR'S TARP: blue/silver material is on a building LEFT or RIGHT of center, NOT on centered building.
  3. SWIMMING POOL: blue water-filled oval/rectangular shape clearly in the YARD at GROUND LEVEL — separated from roof by grass/patio/concrete.
Set is_fp=true ONLY for these three cases.
Answer ONLY with valid JSON: {"is_fp":false,"reason":"none"}"""

PRESCREEN_PROMPT_BLUE = """{YEAR_CONTEXT}Aerial satellite image of a Southern California residential property.
Pre-scan detected blue or silver material on/near the CENTER house roof.

Score 1-10:

▸ SCORE 8-10 — CALIENTE (real tarp confirmed):
  - Blue/navy/silver material DRAPED over the roof surface of the CENTER house
  - Score 9-10: physical tarp evidence visible (wrinkles/folds, sandbag weights, material hanging over roofline)
  - Score 8: blue/silver clearly on roof but no strong physical tarp evidence yet

▸ SCORE 5-7 — POSIBLE (ambiguous, worth a visit):
  - Bluish/grayish discoloration on roof that COULD be aged tarp but lacks clear physical evidence
  - OR tile roof with genuinely missing/broken tiles AND the blue detection was noise
  - Score 5: uncertain. Score 6-7: convincing deterioration or partial tarp visibility.

▸ SCORE 1-4 — LIMPIO (false positive):
  - Pool in YARD at ground level (not on roof) → score 2
  - Painted metal roof: uniform color, clean straight seams → score 3
  - Fumigation tent: multi-color stripes wrapping house walls AND roof → score 2
  - Neighbor: material on adjacent building, NOT centered house → score 2
  - Solar panels, skylights, AC units — no damage → score 3-4

AGE FACTOR: House built before 1970 + borderline score 4-5 → add 1 point.

Answer ONLY with valid JSON (no markdown):
{"is_residential":true,"tarp_visible":false,"tarp_evidence":"none","tarp_color":"none","roof_type":"tile","flat_patches":false,"flat_water_stains":false,"missing_tiles":false,"condition":"fair","score":3,"description":"<describe what you see>"}"""

PRESCREEN_PROMPT = """{YEAR_CONTEXT}Aerial satellite image of a Southern California residential property.
Pre-scan flagged possible roof damage (holes, missing sections, or deterioration) on the CENTER house.

Score 1-10:

▸ SCORE 8-10 — CALIENTE (clear structural damage):
  - Tile roof: CLEARLY VISIBLE GAPS — black holes/voids where tiles are missing
  - Flat roof: exposed substrate or multiple large patches >20% coverage
  - Any roof: blue/silver tarp draped over any section
  - Score 8: serious damage obvious. Score 9-10: tarp confirmed OR extensive structural failure.

▸ SCORE 5-7 — POSIBLE (genuinely deteriorated, worth a visit):
  - Tile roof: irregular tile pattern with some broken/displaced tiles visible
  - Flat roof: patches of clearly DIFFERENT color material, OR water stain rings
  - CRITICAL: score 5-7 ONLY for REAL deterioration (not just dirt or normal weathering)
  - Score 5: possible but uncertain. Score 6-7: multiple visible signs.

▸ SCORE 1-4 — LIMPIO (normal roof):
  - Uniform surface, normal weathering, no damage evidence
  - Score 1-3: clearly normal. Score 4: minor signs, likely just age/weather.

── TARP RULE ──
tarp_visible=true if blue or silver material is ON the CENTER house roof.
tarp_evidence: "wrinkles" | "sandbags" | "draped_edges" | "none"
Tarp with evidence → score 9-10. Tarp without evidence → score 8.

AGE FACTOR: House built before 1970 + borderline score 4-5 → add 1 point.

Answer ONLY with valid JSON (no markdown):
{"is_residential":true,"tarp_visible":false,"tarp_evidence":"none","tarp_color":"none","roof_type":"flat|tile|shingle|metal|unknown","flat_patches":false,"flat_water_stains":false,"missing_tiles":false,"condition":"new|good|fair|poor|critical","score":3,"description":"roof type first, then specific damage or reason it looks normal"}"""

DETAIL_PROMPT = """Southern California roofing inspector with aerial satellite + street view of a property.
The aerial flagged this property. Confirm or deny from street level.

FLAT ROOF from street view:
- Normal: smooth wall edge, no sagging, no visible damage at roofline
- Concerning: visible patches at roof edge, sagging sections, stains from roof drainage failure

TILE ROOF from street view:
- Normal: tiles appear intact even if weathered/faded
- Concerning: clearly cracked or missing tiles visible from street angle
- Tarp: blue or silver material draped over part of roof

Answer ONLY with valid JSON:
{"roof_type_confirmed":"flat|tile|shingle|metal","tarp_confirmed":false,"tarp_color":"none|blue|silver","damage_visible_from_street":false,"damage_description":"what specific damage you see or 'no damage visible'","condition_street":"good|fair|poor","score_final":3}

score_final: 1-4=looks normal, 5-7=genuine deterioration from street, 8-9=serious damage, 10=tarp/failure confirmed
NOTE: score 5-7 requires REAL signs of deterioration — not just old age or dirt."""

def make_step2_prompt(year_built, is_blue=False):
    yr_ctx = f"Property year built: {year_built}.\n" if year_built else ""
    return (PRESCREEN_PROMPT_BLUE if is_blue else PRESCREEN_PROMPT).replace("{YEAR_CONTEXT}", yr_ctx)

# ─── SCORING (con ambiguity-cap integrado) ──────────────────────────────────────
_AMBIG_SIGNALS = [
    "lacks clear physical evidence", "lack of physical evidence", "no physical evidence",
    "ambiguous", "warranting a visit", "warrant a visit", "uncertain", "inconclusive",
    "cannot confirm", "no visible wrinkle", "no wrinkle", "without wrinkle",
    "no clear physical", "may not be a tarp", "not certain",
]

def compute_score(binary, detail=None, blue_confirmed=False, full_desc=""):
    s          = int(binary.get("score", 0))
    roof_type  = (binary.get("roof_type") or "unknown").lower()
    tarp_color = (binary.get("tarp_color") or "none").lower().strip()
    if tarp_color not in ("blue", "silver", "none"):
        tarp_color = "blue" if "blue" in tarp_color else ("silver" if "silver" in tarp_color else "none")

    # FP safeguard
    fp_type = (binary.get("false_positive_type") or "none").lower()
    if fp_type in ("pool", "painted_metal", "tent", "neighbor"):
        return min(3, s)

    if roof_type == "flat":
        if not (binary.get("flat_patches") or binary.get("flat_water_stains") or
                (binary.get("tarp_visible") and tarp_color in ("blue", "silver"))):
            s = min(s, 4)

    if roof_type == "tile":
        if not (binary.get("missing_tiles") or binary.get("broken_tiles") or
                (binary.get("tarp_visible") and tarp_color in ("blue", "silver"))):
            s = min(s, 5)

    if binary.get("tarp_visible") and tarp_color in ("blue", "silver"):
        tarp_evidence = (binary.get("tarp_evidence") or "none").lower()
        if tarp_evidence == "none":
            desc = (binary.get("description") or "").lower()
            flat_sigs = ["uniform", "smooth", "flat cover", "no visible wrinkle",
                         "without wrinkle", "uniformly", "flat surface", "no evidence"]
            if any(sig in desc for sig in flat_sigs):
                s = min(s, 6)
            elif blue_confirmed:
                s = max(s, 7)
            else:
                s = min(s, 6)
        else:
            s = max(s, 9)
    elif binary.get("tarp_visible") and tarp_color not in ("blue", "silver", "none"):
        s = min(s, 6)

    if detail:
        sc    = detail.get("score_final", 0)
        tarp_c = (detail.get("tarp_color") or "none").lower()
        if detail.get("tarp_confirmed") and tarp_c in ("blue", "silver"):
            s = max(s, 9)
        if detail.get("damage_visible_from_street"):
            s = max(s, 6)
        elif sc > 0:
            s = min(s, max(sc, s - 2))

    s = min(10, max(1, s))

    # Ambiguity cap: descripción con incertidumbre → máx 7 (POSIBLE, nunca CALIENTE)
    if s >= 8 and full_desc and any(sig in full_desc.lower() for sig in _AMBIG_SIGNALS):
        s = 7

    return s

# ─── CSV WRITERS ───────────────────────────────────────────────────────────────
def _write_row(path, record):
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(record)

def _make_row(prop, score, tarp, tarp_color, roof_type, full_desc, estado):
    lat, lng = prop["lat"], prop["lng"]
    tarp_str = ""
    if tarp:
        tarp_str = "SÍ (azul)" if "blue" in (tarp_color or "") else ("SÍ (plata)" if "silver" in (tarp_color or "") else "SÍ")
    return {
        "Score":           score,
        "Lona":            tarp_str,
        "Dirección":       prop.get("address", ""),
        "Año construcción": "",
        "Lat":             lat,
        "Lng":             lng,
        "Link Google Maps": f"https://maps.google.com/?q={lat},{lng}",
        "Tipo techo":      roof_type,
        "Descripción del daño": full_desc[:200],
        "Folio":           prop.get("ain", ""),
        "Estado":          estado,
        "Contactado (sí/no)": "",
        "Daño confirmado (sí/no/no visible)": "",
        "Notas del setter": "",
    }

# ─── GITHUB PUSH ───────────────────────────────────────────────────────────────
def push_to_github(local_path, gh_path, quiet=False):
    if not GITHUB_TOKEN or not local_path.exists():
        return
    try:
        hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        api  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}"
        ex   = requests.get(api, headers=hdrs, timeout=15)
        sha  = ex.json().get("sha", "") if ex.status_code == 200 else ""
        content = base64.b64encode(local_path.read_bytes()).decode()
        payload = {"message": f"auto: CA GIS scan {datetime.now().strftime('%Y-%m-%d')}", "content": content}
        if sha:
            payload["sha"] = sha
        r = requests.put(api, headers=hdrs, json=payload, timeout=30)
        if not quiet:
            if r.status_code in (200, 201):
                log(f"✅ GitHub push: {gh_path}")
            else:
                log(f"⚠️ GitHub push failed {gh_path}: {r.status_code}")
    except Exception as e:
        log(f"⚠️ GitHub error: {e}")

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log("=" * 65)
    log(f"CA GIS SCANNER (LA County) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log(f"Bbox: {BBOX} | MAX={MAX_ANALYZED} | DRY_RUN={DRY_RUN}")
    log(f"Límite IA: ${MAX_AI_SPEND:.2f} | Pipeline: STEP1→FP_CHECK→STEP2→scoring+ambiguity_cap")
    log("=" * 65)

    # Cargar cache de AIns ya analizados
    ain_cache = load_ain_cache()
    log(f"Cache: {len(ain_cache)} propiedades ya analizadas")

    # Inicializar CSVs con headers (aunque no haya leads, el archivo existe para push)
    for csv_path in (LEADS_CSV, REVISAR_CSV):
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=CSV_FIELDNAMES).writeheader()

    # Descargar propiedades del GIS de LA County (paginación hasta GIS_MAX_RECORDS)
    log(f"Descargando propiedades del GIS de LA County (cap={GIS_MAX_RECORDS})...")
    all_props = fetch_properties_la(BBOX, max_records=GIS_MAX_RECORDS)
    log(f"GIS total: {len(all_props)} propiedades descargadas (cap={GIS_MAX_RECORDS})")

    if DRY_RUN:
        log("DRY_RUN=1 — primeras 5 propiedades y saliendo:")
        for p in all_props[:5]:
            log(f"  {p['address']} | lat={p['lat']:.5f}, lng={p['lng']:.5f} | AIN={p['ain']}")
        return

    new_props = [p for p in all_props if p.get("ain") not in ain_cache]
    log(f"Propiedades nuevas: {len(new_props)} | Procesando hasta {MAX_ANALYZED}")

    analyzed = 0
    for i, prop in enumerate(new_props):
        if analyzed >= MAX_ANALYZED:
            log(f"⏹ MAX_ANALYZED={MAX_ANALYZED} alcanzado")
            break
        ai_spend = STATS["step1_usd"] + STATS.get("step2_usd", 0.0)
        if ai_spend >= MAX_AI_SPEND:
            log(f"⛔ Límite IA alcanzado (${ai_spend:.3f} >= ${MAX_AI_SPEND})")
            break

        lat, lng = prop["lat"], prop["lng"]
        ain      = prop.get("ain", "")
        addr     = prop.get("address", f"{lat},{lng}")

        logp(f"[{analyzed+1}|{STATS['hot']}/{MAX_ANALYZED}] {addr}\n")

        # Imagen satélite zoom 21
        sat = satellite(lat, lng, zoom=21)
        if not sat:
            logp(f"  ⚠ sat fail\n")
            STATS["errors"] += 1
            ain_cache.add(ain)
            analyzed += 1
            time.sleep(1)
            continue

        # STEP1 — binario rápido
        b1 = ai_call([sat], STEP1_PROMPT, max_tokens=80, detail="low", bucket="step1")
        if "error" in b1:
            logp(f"  ⚠ STEP1 error: {b1['error']}\n")
            STATS["errors"] += 1
            ain_cache.add(ain)
            analyzed += 1
            time.sleep(2)
            continue

        blue_flag  = bool(b1.get("blue_or_silver_on_roof"))
        holes_flag = bool(b1.get("visible_holes_or_missing_sections"))
        detr_flag  = bool(b1.get("visible_deterioration"))
        obv_fp     = bool(b1.get("obvious_false_positive"))

        flags = ("🔵" if blue_flag else "") + ("⚫" if holes_flag else "") + ("🟤" if detr_flag else "")
        logp(f"  paso1→ {'CANDIDATO '+flags if (blue_flag or holes_flag or detr_flag) and not obv_fp else 'clean'}\n")

        # FP check adicional para casos azules no obvios
        if blue_flag and not obv_fp:
            fp2 = ai_call([sat], FP_CHECK_PROMPT, max_tokens=40, detail="low", bucket="step1")
            if fp2.get("is_fp"):
                logp(f"  FP_CHECK→ FP ({fp2.get('reason','?')}) — descartado\n")
                ain_cache.add(ain)
                STATS["clean"] += 1
                analyzed += 1
                time.sleep(1)
                continue

        if obv_fp or not (blue_flag or holes_flag or detr_flag):
            ain_cache.add(ain)
            STATS["clean"] += 1
            analyzed += 1
            time.sleep(1)
            continue

        # STEP2 — scoring detallado
        step2_prompt = make_step2_prompt(None, is_blue=blue_flag)
        binary = ai_call([sat], step2_prompt, max_tokens=200, detail="high", bucket="step2")
        if "error" in binary:
            logp(f"  ⚠ STEP2 error: {binary['error']}\n")
            ain_cache.add(ain)
            STATS["errors"] += 1
            analyzed += 1
            time.sleep(2)
            continue

        # Street view (si disponible)
        detail = {}
        has_sv, sv_date = sv_available(lat, lng)
        if has_sv:
            sv0  = streetview(lat, lng, 0)
            sv90 = streetview(lat, lng, 90)
            sv_imgs = [i for i in [sv0, sv90] if i]
            if sv_imgs:
                all_imgs = [sat] + sv_imgs
                det = ai_call(all_imgs, DETAIL_PROMPT, max_tokens=200, detail="low", bucket="step2")
                if "error" not in det:
                    detail = det

        desc_main   = binary.get("description", "")
        desc_detail = detail.get("damage_description", "")
        full_desc   = (desc_main + " " + desc_detail).strip()

        final = compute_score(binary, detail, blue_confirmed=blue_flag, full_desc=full_desc)

        tarp      = binary.get("tarp_visible", False) or detail.get("tarp_confirmed", False)
        tarp_color = binary.get("tarp_color", "none")
        roof_type  = binary.get("roof_type", "unknown")

        if final >= 8:
            STATS["hot"] += 1
            logp(f"  ✅ CALIENTE #{STATS['hot']} score={final} | {addr}\n")
            row = _make_row(prop, final, tarp, tarp_color, roof_type, full_desc, "revisar")
            _write_row(LEADS_CSV, row)
        elif final >= 5:
            STATS["posible"] += 1
            logp(f"  🟡 POSIBLE #{STATS['posible']} score={final} | {addr}\n")
            row = _make_row(prop, final, tarp, tarp_color, roof_type, full_desc, "revisar")
            _write_row(REVISAR_CSV, row)
        else:
            STATS["clean"] += 1

        ain_cache.add(ain)
        analyzed += 1

        if analyzed % 25 == 0:
            save_ain_cache(ain_cache)
            ai_sp = STATS["step1_usd"] + STATS.get("step2_usd", 0.0)
            log(f"  [{analyzed}/{MAX_ANALYZED}] hot={STATS['hot']} posible={STATS['posible']} | IA=${ai_sp:.3f}")

        time.sleep(1.5)

    save_ain_cache(ain_cache)

    # Push a GitHub
    push_to_github(LEADS_CSV,   f"data/leads_{STATE_TAG}.csv")
    push_to_github(REVISAR_CSV, f"data/revisar_{STATE_TAG}.csv")

    # Reporte final
    ai_spend    = STATS["step1_usd"] + STATS.get("step2_usd", 0.0)
    total_spend = ai_spend + STATS["maps_usd"]
    log("")
    log("=" * 65)
    log(f"REPORTE FINAL — CA GIS Scanner")
    log(f"  Casas analizadas:   {analyzed}")
    log(f"  🔥 Calientes (8-10): {STATS['hot']}  → leads_CA.csv")
    log(f"  🟡 Posibles (5-7):   {STATS['posible']}  → revisar_CA.csv")
    log(f"  Limpias:            {STATS['clean']}")
    log(f"  Errores:            {STATS['errors']}")
    log(f"  Costo IA:           ${ai_spend:.4f}")
    log(f"  Costo Maps:         ${STATS['maps_usd']:.4f}")
    log(f"  TOTAL estimado:     ${total_spend:.4f}")
    log(f"  Modelo:             {AI_MODEL} vía OpenRouter")
    log("=" * 65)

    # run_history_CA.csv
    hist_path = OUT_DIR / "run_history_CA.csv"
    hist_fields = ["fecha", "analiz", "calientes", "posibles", "limpias", "errores",
                   "ia_usd", "maps_usd", "total_usd", "modelo", "bbox"]
    hist_exists = hist_path.exists() and hist_path.stat().st_size > 0
    with open(hist_path, "a", newline="", encoding="utf-8") as hf:
        hw = csv.DictWriter(hf, fieldnames=hist_fields)
        if not hist_exists:
            hw.writeheader()
        hw.writerow({
            "fecha":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "analiz":    analyzed,
            "calientes": STATS["hot"],
            "posibles":  STATS["posible"],
            "limpias":   STATS["clean"],
            "errores":   STATS["errors"],
            "ia_usd":    f"{ai_spend:.4f}",
            "maps_usd":  f"{STATS['maps_usd']:.4f}",
            "total_usd": f"{total_spend:.4f}",
            "modelo":    AI_MODEL,
            "bbox":      f"{BBOX['xmin']},{BBOX['ymin']},{BBOX['xmax']},{BBOX['ymax']}",
        })

if __name__ == "__main__":
    main()
