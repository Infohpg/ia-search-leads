#!/usr/bin/env python3
"""
HPG Overnight Roof Scanner v3
- Principal: GPT-4o-mini (detail:low) — ~$1.50 / 2000 casas
- Fallback:  OpenRouter free (nemotron, gemma)
- Límites duros: MAX_API_CALLS y MAX_SPEND_USD (sin excepciones)
"""

import os, requests, base64, json, re, time, csv, random
from datetime import datetime
from pathlib import Path

# ─── CREDENCIALES — leer de env vars (requerido en producción/Docker) ─────────
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OR_KEY        = os.environ.get("OPENROUTER_API_KEY", "")
MAPS_KEY      = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "Infohpg/ia-search-leads")
if not OPENAI_KEY or not MAPS_KEY:
    raise RuntimeError("OPENAI_API_KEY y GOOGLE_MAPS_API_KEY son requeridas (vars de entorno)")

# ─── LÍMITES DUROS — SIN EXCEPCIONES ──────────────────────────────────────────
MAX_API_CALLS  = 2200
MAX_SPEND_USD  = 3.00
IN_PRICE       = 0.15 / 1_000_000   # $ por token entrada (gpt-4o-mini)
OUT_PRICE      = 0.60 / 1_000_000   # $ por token salida  (gpt-4o-mini)
SPEND_ALERT_2X = 0.005              # alerta si 1 sola llamada supera este monto (~2× detail)

# Precios Google Maps API (para estimado de costo total)
MAPS_SAT_PRICE = 0.002   # $2/1000 Static Maps
MAPS_SV_PRICE  = 0.007   # $7/1000 Street View Static

# ─── ARCHIVOS ─────────────────────────────────────────────────────────────────
OUT_DIR           = Path("./scan_results"); OUT_DIR.mkdir(exist_ok=True)
LIVE_FILE         = OUT_DIR / "overnight_leads.json"
LOG_FILE          = OUT_DIR / "overnight_log.txt"
FOLIO_CACHE_FILE  = OUT_DIR / "analyzed_folios.json"
LEADS_CSV_FILE    = OUT_DIR / "leads_para_ventas.csv"
RUN_HISTORY_FILE  = OUT_DIR / "run_history.csv"

CSV_FIELDNAMES = [
    "Score", "Lona", "Dirección", "Año construcción", "Lat", "Lng",
    "Link Google Maps", "Tipo techo", "Descripción del daño", "Folio",
    "Estado",
    "Contactado (sí/no)", "Daño confirmado (sí/no/no visible)", "Notas del setter"
]

RUN_HISTORY_FIELDNAMES = [
    "fecha", "zips_usados", "casas_analizadas", "candidatos_detail_high",
    "leads_nuevos", "leads_totales", "gasto_usd", "sat_fail", "ai_fail",
    "errores", "status"
]

def append_lead_to_csv(record):
    """Appends a single qualified lead to leads_para_ventas.csv (creates with header if new)."""
    write_header = not LEADS_CSV_FILE.exists() or LEADS_CSV_FILE.stat().st_size == 0
    tarp = ""
    if record.get("lona_visible"):
        color = (record.get("lona_color") or "").lower()
        tarp = "SÍ (azul)" if "blue" in color else ("SÍ (plata)" if "silver" in color else "SÍ")
    lat, lng = record.get("lat", ""), record.get("lng", "")
    row = {
        "Score":                          record.get("score_urgencia", ""),
        "Lona":                           tarp,
        "Dirección":                      record.get("address", ""),
        "Año construcción":               record.get("year_built", ""),
        "Lat":                            lat,
        "Lng":                            lng,
        "Link Google Maps":               record.get("gmaps", f"https://maps.google.com/?q={lat},{lng}"),
        "Tipo techo":                     record.get("tipo_techo", ""),
        "Descripción del daño":           record.get("descripcion", ""),
        "Folio":                          record.get("folio", ""),
        "Estado":                         record.get("estado", "revisar"),
        "Contactado (sí/no)":             "",
        "Daño confirmado (sí/no/no visible)": "",
        "Notas del setter":               "",
    }
    with open(LEADS_CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ─── PARÁMETROS DE CORRIDA ────────────────────────────────────────────────────
TARGET_LEADS = 9999
MAX_ANALYZED = int(os.environ.get("SCANNER_MAX", "330"))  # override con SCANNER_MAX env var (testing)
DRY_RUN      = os.environ.get("DRY_RUN", "0").strip() == "1"  # no push a GitHub ni webhooks

# ─── MODELOS ──────────────────────────────────────────────────────────────────
# STEP1 (filtro masivo, barato): siempre gpt-4o-mini + OR fallbacks. No configurable.
MODELS = [
    ("openai",     "gpt-4o-mini"),
    ("openrouter", "nvidia/nemotron-nano-12b-v2-vl:free"),
    ("openrouter", "google/gemma-4-31b-it:free"),
    ("openrouter", "google/gemma-4-26b-a4b-it:free"),
    # MUERTOS: moonshotai/kimi-k2.6:free (404), google/gemma-4-27b-it:free (400)
]

# STEP2 + STEP3 (capa inteligente): configurable vía env var.
# Para cambiar: SMART_MODEL=claude-haiku-4-5-20251001 SMART_PROVIDER=claude
# Por defecto: gpt-4o-mini (igual que STEP1 hasta tener la key de Claude)
SMART_PROVIDER  = os.environ.get("SMART_PROVIDER",  "openai")
SMART_MODEL_ID  = os.environ.get("SMART_MODEL",     "gpt-4o-mini")
MODELS_SMART = [
    (SMART_PROVIDER, SMART_MODEL_ID),
    ("openrouter",   "nvidia/nemotron-nano-12b-v2-vl:free"),
    ("openrouter",   "google/gemma-4-31b-it:free"),
]

# ─── MODO HURACÁN ─────────────────────────────────────────────────────────────
# Cuando activo: 100% budget a HURRICANE_ZIPS, re-analiza "clean" anteriores a HURRICANE_DATE
HURRICANE_ZIPS = []    # e.g. ["33013","33012"] — vacío = modo normal
HURRICANE_DATE = ""    # e.g. "2026-09-15" — re-analizar clean folios antes de esta fecha

# ─── TODAS LAS ZIPs de Miami-Dade con inventario pre-2000 SFH ────────────────
# Inventario validado via GIS Miami-Dade ArcGIS REST API (returnCountOnly)
ALL_ZIPS = {
    # Hialeah / Hialeah Gardens / Miami Lakes
    "33010": 3682, "33012": 7986, "33013": 6409, "33016": 2901, "33018": 6548,
    # Opa-locka / Carol City / NW Miami
    "33054": 5749, "33055": 4532, "33056": 5319,
    # Little Havana / SW Miami / Flagami
    "33125": 3217, "33126": 5274, "33127": 2516, "33128": 1023,
    "33134": 7488, "33135": 3892, "33136": 2184,
    # Little Haiti / Liberty City / Allapattah
    "33137": 3428, "33138": 4332, "33142": 4809, "33147": 8299, "33150": 3563,
    # Coconut Grove / South Miami / Coral Gables
    "33133": 3154, "33143": 2984, "33144": 4109, "33155": 10892,
    # Kendall / Westchester / West Miami-Dade (grandes — mayoría inexplorados)
    "33165": 12933, "33172": 3109, "33174": 3987, "33175": 9468, "33176": 10184,
    "33177": 10956, "33182": 2614, "33183": 3452, "33184": 3814, "33185": 4125,
    "33186": 10511, "33187": 2983, "33193": 3127, "33194": 2841,
    # South Miami-Dade / Cutler Bay / Palmetto Bay
    "33157": 14684, "33189": 2418, "33196": 2619,
    # Homestead / Florida City
    "33030": 3419, "33031": 2847, "33032": 2561, "33033": 5896,
    # North Miami / Aventura / NE
    "33161": 3571, "33162": 3842,
}

ZIP_STATS_FILE = OUT_DIR / "zip_stats.json"

# ─── ESTADÍSTICAS GLOBALES ────────────────────────────────────────────────────
STATS = {"api_calls": 0, "spend_usd": 0.0, "last_call_spend": 0.0,
         "sat_calls": 0, "sv_calls": 0}

def _track_spend(usage):
    """Registra gasto de una respuesta OpenAI. Retorna costo de esa llamada."""
    if not usage:
        STATS["last_call_spend"] = 0.0
        return 0.0
    cost = usage.get("prompt_tokens", 0) * IN_PRICE + usage.get("completion_tokens", 0) * OUT_PRICE
    STATS["spend_usd"]       += cost
    STATS["last_call_spend"]  = cost
    return cost

def _check_limits():
    """Retorna mensaje si un límite duro está alcanzado, None si OK."""
    if STATS["api_calls"] >= MAX_API_CALLS:
        return f"MAX_API_CALLS={MAX_API_CALLS} (realizadas: {STATS['api_calls']})"
    if STATS["spend_usd"] >= MAX_SPEND_USD:
        return f"MAX_SPEND_USD=${MAX_SPEND_USD:.2f} (gastado: ${STATS['spend_usd']:.4f})"
    return None

def _spend_tag():
    """Etiqueta de gasto compacta para líneas de log."""
    if STATS["last_call_spend"] == 0.0:
        return f"free | acum ${STATS['spend_usd']:.4f}"
    return f"+${STATS['last_call_spend']:.5f} | acum ${STATS['spend_usd']:.4f}"


# ─── LOG ──────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def logp(msg):
    """Log sin timestamp — detalle de propiedad individual (stdout + archivo)."""
    print(msg, end="", flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(msg)


# ─── SCORING ──────────────────────────────────────────────────────────────────

def compute_score(binary, detail=None, blue_confirmed=False):
    s          = int(binary.get("score", 0))
    roof_type  = (binary.get("roof_type")  or "unknown").lower()
    tarp_color = (binary.get("tarp_color") or "none").lower().strip()
    # Normalizar valores compuestos que el modelo retorna literalmente del schema hint
    # Ej: "blue|silver|none" → "blue", "blue/silver" → "blue"
    if tarp_color not in ("blue", "silver", "none"):
        if "blue" in tarp_color:
            tarp_color = "blue"
        elif "silver" in tarp_color:
            tarp_color = "silver"
        else:
            tarp_color = "none"

    # Safeguard: si el modelo identifica explícitamente una categoría de FP → hard cap 3
    fp_type = (binary.get("false_positive_type") or "none").lower().strip()
    if fp_type in ("pool", "painted_metal", "tent", "neighbor"):
        return min(3, s)

    if roof_type == "flat":
        has_damage = (binary.get("flat_patches") or binary.get("flat_water_stains") or
                      (binary.get("tarp_visible") and tarp_color in ("blue", "silver")))
        if not has_damage:
            s = min(s, 4)

    if roof_type == "tile":
        has_damage = (binary.get("missing_tiles") or binary.get("broken_tiles") or
                      (binary.get("tarp_visible") and tarp_color in ("blue", "silver")))
        if not has_damage:
            s = min(s, 5)

    if binary.get("tarp_visible") and tarp_color in ("blue", "silver"):
        tarp_evidence = (binary.get("tarp_evidence") or "none").lower()
        if tarp_evidence == "none":
            # Anti flat-covering: superficie lisa/uniforme sin arrugas → puede ser piscina,
            # techo pintado, o membrana plana. Cap en 6 aunque blue_confirmed=True.
            desc = (binary.get("description") or "").lower()
            flat_signals = ["uniform", "smooth", "flat cover", "may not be a traditional",
                            "no visible wrinkle", "without wrinkle", "without edge",
                            "uniformly", "flat surface", "no evidence"]
            if any(sig in desc for sig in flat_signals):
                s = min(s, 6)  # ambiguo — requiere verificación en persona
            elif blue_confirmed:
                # Paso 1 confirmó azul en techo Y no hay señales de superficie lisa
                s = max(s, 7)
            else:
                s = min(s, 6)
        else:
            s = max(s, 9)  # evidencia física (wrinkles/sandbags/draped_edges) → lona real
    elif binary.get("tarp_visible") and tarp_color not in ("blue", "silver", "none"):
        s = min(s, 6)

    if detail:
        sc     = detail.get("score_final", 0)
        tarp_c = (detail.get("tarp_color") or "none").lower()
        if detail.get("tarp_confirmed") and tarp_c in ("blue", "silver"):
            s = max(s, 9)
        if detail.get("damage_visible_from_street"):
            s = max(s, 6)
        elif sc > 0:
            s = min(s, max(sc, s - 2))

    return min(10, max(1, s))


# ─── GOOGLE MAPS ──────────────────────────────────────────────────────────────

def get_img(url, params):
    try:
        r = requests.get(url, params=params, timeout=18)
        if r.status_code == 200 and len(r.content) > 4000:
            return base64.b64encode(r.content).decode()
    except: pass
    return None

def satellite(lat, lng, zoom=21):
    img = get_img("https://maps.googleapis.com/maps/api/staticmap",
        {"center": f"{lat},{lng}", "zoom": zoom, "size": "512x512",
         "maptype": "satellite", "key": MAPS_KEY})
    if img:
        STATS["sat_calls"] += 1
    return img

def streetview(lat, lng, heading=0, fov=70):
    img = get_img("https://maps.googleapis.com/maps/api/streetview",
        {"size": "512x512", "location": f"{lat},{lng}", "heading": heading,
         "pitch": 10, "fov": fov, "key": MAPS_KEY, "return_error_code": "true"})
    if img:
        STATS["sv_calls"] += 1
    return img

def sv_available(lat, lng):
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": f"{lat},{lng}", "key": MAPS_KEY}, timeout=10)
        d = r.json()
        return d.get("status") == "OK", d.get("date", "?")
    except: return False, "?"


# ─── FOLIO CACHE ──────────────────────────────────────────────────────────────

def load_folio_cache():
    if FOLIO_CACHE_FILE.exists():
        try: return json.loads(FOLIO_CACHE_FILE.read_text())
        except: pass
    return {}

def save_folio_cache(cache):
    FOLIO_CACHE_FILE.write_text(json.dumps(cache, indent=2))

def cache_folio(cache, folio, result, score=0, reason=""):
    if not folio: return
    entry = {"result": result, "score": score,
             "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if reason:
        entry["reason"] = reason[:80]
    cache[folio] = entry
    save_folio_cache(cache)

def restore_folio_cache_from_github():
    """Startup: si no hay cache local, restaurar desde backups/ en GitHub."""
    if FOLIO_CACHE_FILE.exists() and FOLIO_CACHE_FILE.stat().st_size > 0:
        return
    if not GITHUB_TOKEN:
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/backups/analyzed_folios.json",
            headers=headers, timeout=15)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"])
            FOLIO_CACHE_FILE.write_bytes(content)
            cache = json.loads(content)
            log(f"✅ Cache restaurado desde GitHub backup ({len(cache)} folios analizados)")
        elif r.status_code == 404:
            log("Cache backup no existe en GitHub todavía — empezando sin cache")
        else:
            log(f"⚠️ restore cache HTTP {r.status_code}")
    except Exception as e:
        log(f"⚠️ restore cache error: {e}")

def backup_folio_cache_to_github():
    """Post-run: push analyzed_folios.json a backups/ en GitHub para recuperación ante pérdida de volumen."""
    if not GITHUB_TOKEN or not FOLIO_CACHE_FILE.exists():
        log("⚠️ Cache backup skipped (no token o archivo no existe)")
        return
    try:
        cache = json.loads(FOLIO_CACHE_FILE.read_text())
        content = base64.b64encode(FOLIO_CACHE_FILE.read_bytes()).decode()
        fecha = datetime.now().strftime("%Y-%m-%d")
        api_path = f"https://api.github.com/repos/{GITHUB_REPO}/contents/backups/analyzed_folios.json"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        existing = requests.get(api_path, headers=headers, timeout=15).json()
        sha = existing.get("sha", "")
        payload = {"message": f"backup: folio cache {fecha} ({len(cache)} folios)", "content": content}
        if sha:
            payload["sha"] = sha
        r = requests.put(api_path, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log(f"✅ Cache backup pushed to GitHub ({len(cache)} folios analizados)")
        else:
            log(f"⚠️ Cache backup push failed: HTTP {r.status_code} — {r.json().get('message','?')[:60]}")
    except Exception as e:
        log(f"⚠️ Cache backup error: {e}")

def restore_csv_from_github():
    """Startup: si no hay CSV local, restaurar desde data/ en GitHub para mantener historial acumulativo."""
    if LEADS_CSV_FILE.exists() and LEADS_CSV_FILE.stat().st_size > 0:
        return
    if not GITHUB_TOKEN:
        return
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/leads_para_ventas.csv",
            headers=headers, timeout=15)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"])
            LEADS_CSV_FILE.write_bytes(content)
            lines = len(content.decode('utf-8-sig', errors='replace').strip().split('\n'))
            log(f"✅ CSV restaurado desde GitHub ({lines - 1} leads históricos)")
        elif r.status_code == 404:
            log("CSV no existe en GitHub todavía — empezando limpio")
        else:
            log(f"⚠️ restore CSV HTTP {r.status_code}")
    except Exception as e:
        log(f"⚠️ restore CSV error: {e}")


# ─── AI CALL ──────────────────────────────────────────────────────────────────

def ai_call(images, prompt, max_tokens=300, detail="low", model_list=None):
    """Llama al modelo de IA con las imágenes y el prompt dado.
    model_list: None = usar MODELS (STEP1 barato), MODELS_SMART = capa inteligente.
    detail='high' solo para candidatos azules en paso2 — ~3x costo de imagen.
    Retorna (result_dict, model_label).
    result_dict['error'] existe en caso de fallo; 'error'=='hard_limit' = abortar corrida."""
    raw      = ""
    last_err = "unknown"
    models   = model_list if model_list is not None else MODELS

    for provider, model in models:
        for attempt in range(2):

            # ── Límites duros — verificar ANTES de hacer la llamada ──
            limit_hit = _check_limits()
            if limit_hit:
                return {"error": "hard_limit", "last_err": limit_hit}, "none"

            STATS["api_calls"]       += 1
            STATS["last_call_spend"]  = 0.0

            # ── Construir request según provider ──
            if provider == "claude":
                # Anthropic Messages API
                if not ANTHROPIC_KEY:
                    last_err = "claude/no_key"; break
                content = []
                for img in images:
                    if img:
                        content.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": img}
                        })
                content.append({"type": "text", "text": prompt})
                url     = "https://api.anthropic.com/v1/messages"
                headers = {"x-api-key": ANTHROPIC_KEY,
                           "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}
                body    = {"model": model,
                           "max_tokens": max_tokens,
                           "temperature": 0,
                           "messages": [{"role": "user", "content": content}]}
            elif provider == "openai":
                content = [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": detail}}
                    for img in images if img
                ]
                content.append({"type": "text", "text": prompt})
                url     = "https://api.openai.com/v1/chat/completions"
                headers = {"Authorization": f"Bearer {OPENAI_KEY}",
                           "Content-Type": "application/json"}
                body    = {"model": model,
                           "messages": [{"role": "user", "content": content}],
                           "max_tokens": max_tokens, "temperature": 0}
            else:
                # OpenRouter (cualquier otro provider)
                content = [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                    for img in images if img
                ]
                content.append({"type": "text", "text": prompt})
                url     = "https://openrouter.ai/api/v1/chat/completions"
                headers = {"Authorization": f"Bearer {OR_KEY}",
                           "Content-Type": "application/json", "X-Title": "HPG-Night"}
                body    = {"model": model,
                           "messages": [{"role": "user", "content": content}],
                           "max_tokens": max_tokens, "temperature": 0}

            try:
                r = requests.post(url, headers=headers, json=body, timeout=55)
                d = r.json()

                # Registrar gasto
                if provider == "openai":
                    _track_spend(d.get("usage"))
                elif provider == "claude":
                    usage = d.get("usage", {})
                    # Claude Haiku 4.5: $0.80/1M input, $4.00/1M output
                    IN_PRICE_C  = 0.80  / 1_000_000
                    OUT_PRICE_C = 4.00  / 1_000_000
                    cost = (usage.get("input_tokens", 0) * IN_PRICE_C +
                            usage.get("output_tokens", 0) * OUT_PRICE_C)
                    STATS["spend_usd"]      += cost
                    STATS["last_call_spend"] = cost

                # Detectar errores según provider
                if provider == "claude":
                    if d.get("type") == "error":
                        err_msg  = d.get("error", {}).get("message", "")[:100]
                        err_type = d.get("error", {}).get("type", "")
                        last_err = f"claude/{model} type:{err_type} | {err_msg}"
                        wait = 30 if "rate" in err_type else 6
                        time.sleep(wait if attempt == 0 else wait * 2); continue
                elif "error" in d:
                    err_code = d["error"].get("code")
                    err_msg  = str(d["error"].get("message", ""))[:100]
                    last_err = f"{provider}/{model.split('/')[-1]} code:{err_code} | {err_msg}"
                    wait = (30 if attempt == 0 else 60) if err_code == 429 else (6 if attempt == 0 else 12)
                    time.sleep(wait)
                    continue

                # Extraer texto según provider
                if provider == "claude":
                    raw = (d.get("content") or [{}])[0].get("text", "").strip()
                else:
                    raw = (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

                if not raw:
                    last_err = f"{provider}/{model.split('/')[-1]} empty_response"
                    time.sleep(5); continue

                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]

                return json.loads(raw.strip()), f"{provider}/{model.split('/')[-1]}"

            except json.JSONDecodeError:
                # Intento con regex antes de declarar fallo
                m = re.search(r'\{.*\}', raw.strip(), re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group()), f"{provider}/{model.split('/')[-1]}"
                    except json.JSONDecodeError:
                        pass
                last_err = f"{provider}/{model.split('/')[-1]} json_decode_error"
                break   # json_decode no es 429 — no activa circuit breaker
            except Exception as e:
                last_err = f"{provider}/{model.split('/')[-1]} exception:{str(e)[:60]}"
                time.sleep(8)

    return {"error": "all_models_failed", "last_err": last_err}, "none"


# ─── GIS ──────────────────────────────────────────────────────────────────────

def get_properties(zip_codes, zip_stats=None):
    """Fetch SFH properties from Miami-Dade GIS. Uses per-ZIP offset from zip_stats for pagination."""
    if zip_stats is None:
        zip_stats = {}
    all_props = []
    BASE = "https://gisweb.miamidade.gov/arcgis/rest/services/MD_LandInformation/MapServer/24/query"
    for zc in zip_codes:
        offset = 0 if HURRICANE_ZIPS else zip_stats.get(zc, {}).get("gis_offset", 0)
        try:
            r = requests.get(BASE, params={
                "where": f"TRUE_SITE_ZIP_CODE LIKE '{zc}%' AND (DOR_CODE_CUR LIKE '01%' OR DOR_CODE_CUR LIKE '02%' OR DOR_CODE_CUR LIKE '03%') AND CONDO_FLAG='N'",
                "outFields": "FOLIO,TRUE_SITE_ADDR,TRUE_SITE_CITY,TRUE_SITE_ZIP_CODE,YEAR_BUILT",
                "returnGeometry": "true", "outSR": "4326", "f": "json",
                "resultRecordCount": 500, "resultOffset": offset,
                "orderByFields": "YEAR_BUILT ASC"
            }, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            feats = r.json().get("features", [])
            for feat in feats:
                a = feat.get("attributes", {}); geo = feat.get("geometry", {})
                lat, lng = geo.get("y"), geo.get("x")
                if lat and lng and a.get("TRUE_SITE_ADDR"):
                    all_props.append({
                        "address":    f"{a['TRUE_SITE_ADDR']}, {a.get('TRUE_SITE_CITY','Miami')}, FL {a.get('TRUE_SITE_ZIP_CODE','').replace('-0000','')}",
                        "lat":        lat, "lng": lng,
                        "year_built": a.get("YEAR_BUILT"),
                        "folio":      a.get("FOLIO"),
                        "zip":        zc,
                    })
            log(f"  ZIP {zc}: {len(feats)} props (offset={offset})")
            time.sleep(0.8)
        except Exception as e:
            log(f"  ZIP {zc} error: {e}")

    seen = set(); result = []
    for p in sorted(all_props, key=lambda x: x.get("year_built") or 9999):
        key = f"{p['lat']:.4f},{p['lng']:.4f}"
        if key not in seen:
            seen.add(key); result.append(p)
    return result


# ─── ZIP STATS + SCHEDULER ────────────────────────────────────────────────────

def load_zip_stats():
    if ZIP_STATS_FILE.exists():
        try: return json.loads(ZIP_STATS_FILE.read_text())
        except: pass
    return {}

def save_zip_stats(stats):
    ZIP_STATS_FILE.write_text(json.dumps(stats, indent=2))

def select_run_zips(zip_stats, n_top=5, n_new=3):
    """70/30: top-N established ZIPs by lead rate + N new/under-sampled ZIPs.
    Hurricane mode: always returns HURRICANE_ZIPS unchanged."""
    if HURRICANE_ZIPS:
        return HURRICANE_ZIPS
    established = sorted(
        [(z, zip_stats[z]["leads"] / max(zip_stats[z]["analyzed"], 1))
         for z in ALL_ZIPS if z in zip_stats and zip_stats[z].get("analyzed", 0) >= 100],
        key=lambda x: -x[1]
    )
    new_zips = sorted(
        [z for z in ALL_ZIPS if z not in zip_stats or zip_stats[z].get("analyzed", 0) < 100],
        key=lambda z: -ALL_ZIPS.get(z, 0)
    )
    selected = [z for z, _ in established[:n_top]] + new_zips[:n_new]
    return selected if selected else new_zips[:8] or list(ALL_ZIPS.keys())[:8]


# ─── PROMPTS ──────────────────────────────────────────────────────────────────
# STEP1_PROMPT: llamada binaria rápida — alta recall, sin filtros complejos.
# Costo medido: ~$0.000452/prop (2934 tokens in + 20 out, detail:low).
# Si cualquier flag=true → procede al scoring completo (PRESCREEN_PROMPT).
# Si ambos false → propiedad descartada como limpia sin gastar el scoring completo.
STEP1_PROMPT = """Aerial satellite image. Examine the CENTER house (the building most centered in the image).

DETECTION 1 — TARP: Does the CENTER house ROOF have any covering material ON it?
- Any color: blue, navy, dark navy, silver, metallic, green, black, gray, tan, ANY non-natural color
- Old/weathered tarps look DARK NAVY or almost black — count these as YES
- Any material that looks like it was PLACED on the roof (not part of the original structure)
→ Set blue_or_silver_on_roof = true if YES (field name kept for compatibility)

DETECTION 2 — HOLES/MISSING: Visible black gaps, holes, or missing tiles/shingles on the roof?
→ Set visible_holes_or_missing_sections = true if YES

DETECTION 3 — DETERIORATION: Any of these WITHOUT a tarp?
- Sections of clearly different color/material suggesting patch repairs
- Visible exposed dark underlayment or wood substrate through gaps
- Severely uneven roof surface suggesting collapse or structural failure
→ Set visible_deterioration = true if YES (only clear structural issues, not normal weathering)

BIAS RULE — ALTO RECALL: If you are unsure whether something might be a tarp, damage, or a roof
problem — mark the relevant flag as TRUE. The intelligent scoring layer makes the final call.
Never discard a borderline case here. It is better to send a false positive to STEP2 than to miss
a real damaged roof.

FALSE POSITIVE CHECK — only if blue_or_silver_on_roof is true:
  POOL: oval/rectangular blue water in the YARD at ground level, surrounded by grass/patio? → obvious_false_positive = true
  PAINTED ROOF: the WHOLE roof is one solid permanent color with straight regular seams? → obvious_false_positive = true
  STRIPED TENT: colored stripes (blue+red+yellow) covering walls AND roof like a circus tent? → obvious_false_positive = true
  NEIGHBOR: material is on a building to the LEFT, RIGHT, or CORNER — NOT on the centered house? → obvious_false_positive = true

Respond ONLY with valid JSON (no markdown):
{"blue_or_silver_on_roof":false,"visible_holes_or_missing_sections":false,"obvious_false_positive":false,"visible_deterioration":false}"""

# FP_CHECK_PROMPT: llamada anti-FP de segundo nivel — solo se ejecuta cuando blue_flag=True y obv_fp=False.
# Costo: ~$0.000452/prop. Solo pregunta por los dos FPs que STEP1 confunde: fumigación y vecino.
FP_CHECK_PROMPT = """Aerial satellite image. Blue or silver material was detected on or near the CENTER house.
Is this image clearly one of these three specific false positives?
  1. FUMIGATION TENT: the entire house structure is wrapped in a tent — colored stripes (blue + red/yellow) visible on the walls/sides. Normal roof tiles completely hidden.
  2. NEIGHBOR'S TARP/TENT: the blue/silver material is on a building to the LEFT or RIGHT of center, NOT on the building centered in the image.
  3. SWIMMING POOL: the blue material is a water-filled oval or rectangular shape clearly in the YARD/BACKYARD at GROUND LEVEL — separated from the roof surface by grass, patio, concrete, or yard space. Pools have clean geometric edges and a uniform water-blue color.
Set is_fp=true ONLY for these three cases. For anything else set is_fp=false.
Answer ONLY with valid JSON (no markdown): {"is_fp":false,"reason":"none"}"""

# PRESCREEN_PROMPT_BLUE: paso 1 detectó azul/plateado → exclusión de FPs, default es lona real.
PRESCREEN_PROMPT_BLUE = """Aerial satellite image. Pre-scan detected blue or silver material in this image — possibly on the CENTER house roof. Your task: identify if it is a real tarp/covering ON the CENTER house roof, or a false positive.

STEP 1 — LOCATION CHECK (do this first):
If the blue/silver material is on a NEIGHBOR's roof (not the center house): tarp_visible=false, score=2
If it is a ROUND pool shape in the YARD (not on roof): tarp_visible=false, score=2

STEP 2 — IF MATERIAL IS ON THE CENTER HOUSE ROOF, classify it:
False positive → tarp_visible=false:
- Painted metal roof: uniform color matching entire roof shape with clean straight seams (score 2-3)
- Termite tent: multi-color striped (blue+red+yellow) covering house AND walls (score 2-3)

Real tarp → tarp_visible=true: bright or dark blue, navy, or silver emergency covering draped on roof surface
  tarp_evidence: "wrinkles"=folds visible | "sandbags"=dots/weights at edges | "draped_edges"=hanging over edge | "none"=visible but texture unclear
  tarp_color: "blue" or "silver"
  Score: evidence≠none → 9-10 | evidence=none → 7-8

IMPORTANT: Write a unique description of what you actually observe in THIS image — which section of the roof, estimated coverage area, any edges or texture visible. Do NOT copy the example text below.
Answer ONLY with valid JSON (no markdown):
{"is_residential":true,"tarp_visible":true,"tarp_evidence":"none","tarp_color":"blue","roof_type":"tile","flat_patches":false,"flat_water_stains":false,"missing_tiles":false,"condition":"fair","score":7,"description":"<describe the actual roof section covered, approximate area, and any visible evidence in THIS image>"}"""

# PRESCREEN_PROMPT: para cuando paso 1 detectó holes/damage (sin azul confirmado)
PRESCREEN_PROMPT = """Aerial satellite image of a residential property in Miami/Hialeah, Florida.
Pre-scan flagged possible structural damage (holes or missing sections) on the CENTER house roof. Ignore neighboring properties.

══════════════════════════════════════════
STEP 1 — IDENTIFY ROOF TYPE
══════════════════════════════════════════
flat   = smooth surface, no texture, usually black/gray/white
tile   = textured rows of barrel or flat tiles, usually terracotta/orange/brown
shingle= overlapping flat pieces, dark gray/black textured surface
metal  = shiny, ribbed or standing seam panels

══════════════════════════════════════════
STEP 2 — EVALUATE BY TYPE (different rules for each)
══════════════════════════════════════════

── FLAT ROOFS ──
Normal (score 2-3): Uniform black, gray, or white membrane. Even if dark — this is standard material (EPDM/TPO/modified bitumen). Do NOT flag.
Score 5-6: Visible patches of a clearly DIFFERENT color stuck on the membrane (spot repairs done with different material). OR obvious water stain rings (brown/rust discoloration in irregular circular patterns).
Score 7-8: Multiple large patches covering >20% of the roof. OR exposed substrate (gray boards/wood visible through holes in membrane). OR heavily deteriorated with multiple issues.
Score 9-10: BLUE or SILVER tarp draped over any portion of the flat roof (must pass ALL tarp checks below).

── TILE ROOFS (barrel/flat clay/concrete tile) ──
Normal (score 2-4): Uniform terracotta, orange, brown, or faded tile. Even faded or weathered — this is normal.
Score 5-6: Irregular tile pattern suggesting some broken tiles. Hard to confirm from aerial — flag for street view.
Score 7-8: CLEARLY VISIBLE GAPS (black holes/voids) in the tile field where tiles are missing. Must be actual gaps, not just color variation.
Score 9-10: BLUE or SILVER tarp visible (must pass ALL tarp checks below). OR large section with many missing tiles exposing dark underlayment.

── SHINGLE ROOFS ──
Normal (score 2-3): Uniform dark surface.
Score 6-7: Missing shingle sections visible as lighter patches.
Score 8+: Tarp (must pass ALL tarp checks below), or large exposed areas.

── ANY ROOF TYPE — TARP RULE ──
A tarp is a TEMPORARY emergency covering placed over a damaged roof section.

DETECTION — set tarp_visible=true if you see bright blue or silver-gray material ON THE ROOF of
the central house. When in doubt, flag it (street view will confirm).
Only skip tarp_visible for these two unambiguous cases:
  • A ROUND/circular blue shape in the yard or beside the house = swimming pool, NOT roof damage
  • A multi-color STRIPED (blue+red+yellow) tent wrapping the whole house AND walls = fumigation tent

EVIDENCE — look for physical clues that it is a real draped tarp vs a painted surface:
  "wrinkles"    — creases, folds, rippling or uneven surface visible
  "sandbags"    — dots or linear strips along edges (weights holding the tarp)
  "draped_edges"— material hangs over the roof edge or ridge
  "none"        — surface looks flat/smooth with no physical tarp clues (may be painted roof)

SCORING impact:
  tarp_visible=true + evidence ≠ "none"  →  score 9-10 (real tarp confirmed)
  tarp_visible=true + evidence = "none"  →  score capped at 6 (ambiguous, needs street view)
  Asphalt membrane patches on flat roofs → use flat-roof rules, max score 6, tarp_visible=false

══════════════════════════════════════════
Answer ONLY with valid JSON (no markdown):
{"is_residential":true,"tarp_visible":false,"tarp_evidence":"none","tarp_color":"none|blue|silver","roof_type":"flat|tile|shingle|metal|unknown","flat_patches":false,"flat_water_stains":false,"missing_tiles":false,"condition":"new|good|fair|poor|critical","score":3,"description":"roof type first, then specific damage or why it looks normal"}
══════════════════════════════════════════"""

DETAIL_PROMPT = """Florida roofing inspector with aerial satellite + street view of a Miami property.
The aerial flagged this property. Confirm or deny from street level. Apply the correct rules by roof type.

FLAT ROOF from street view:
- Normal: smooth wall edge, no sagging, no visible damage at roofline
- Concerning: visible patches at roof edge, sagging sections, stains running down exterior walls from roof drainage failure
- DO NOT flag just because the roof is dark/black — that is normal material

TILE ROOF from street view:
- Normal: tiles appear intact even if weathered/faded
- Concerning: clearly cracked or missing tiles visible from street angle
- Tarp: blue or silver material draped over part of roof

Answer ONLY with valid JSON:
{"roof_type_confirmed":"flat|tile|shingle|metal","tarp_confirmed":false,"tarp_color":"none|blue|silver","damage_visible_from_street":false,"damage_description":"what specific damage you see or 'no damage visible'","condition_street":"good|fair|poor","score_final":3}

score_final: 2-3=looks normal, 4-5=minor wear, 6-7=visible damage, 8=serious damage, 9-10=tarp confirmed"""


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    start_time = time.time()
    log(f"=== HPG Overnight Scanner v3 — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    log(f"Principal: {MODELS[0][1]} (detail:low) | Fallbacks: {len(MODELS)-1} modelos OR")
    log(f"Límites duros: MAX_API_CALLS={MAX_API_CALLS} | MAX_SPEND=${MAX_SPEND_USD:.2f}")
    log(f"Target: {TARGET_LEADS} leads | MAX_ANALYZED: {MAX_ANALYZED} | ZIPs totales: {len(ALL_ZIPS)}")
    if HURRICANE_ZIPS:
        log(f"🌀 MODO HURACÁN activo — ZIPs: {HURRICANE_ZIPS} | re-análisis desde: {HURRICANE_DATE or 'N/A'}")
    log("="*65)

    # Restaurar desde GitHub si el volumen está vacío (protege contra pérdida de datos)
    restore_folio_cache_from_github()
    restore_csv_from_github()

    leads = []
    if LIVE_FILE.exists():
        try:
            leads = json.loads(LIVE_FILE.read_text())
            log(f"Leads previos cargados: {len(leads)}")
        except: pass

    folio_cache = load_folio_cache()
    seeded = 0
    for l in leads:
        f = l.get("folio")
        if f and f not in folio_cache:
            folio_cache[f] = {"result": "lead", "score": l.get("score_urgencia", 0),
                              "ts": l.get("analysis_ts", "")}
            seeded += 1
    if seeded:
        save_folio_cache(folio_cache)
        log(f"Cache folios: {len(folio_cache)} entradas ({seeded} sembradas de leads existentes)")
    else:
        log(f"Cache folios: {len(folio_cache)} entradas")

    zip_stats = load_zip_stats()
    run_zips  = select_run_zips(zip_stats)
    log(f"ZIPs seleccionados para esta corrida: {run_zips}")
    log("Cargando propiedades GIS de Miami-Dade...")
    props = get_properties(run_zips, zip_stats)
    log(f"Total propiedades únicas: {len(props)}")
    if props:
        log(f"Rango años: {props[0].get('year_built','?')} – {props[-1].get('year_built','?')}")
    log("")

    analyzed = 0; not_sfh = 0; clean = 0; errors = 0
    sat_fail = 0; ai_fail = 0; detail_fail = 0; step1_clean = 0
    consec_ai_fail = 0; consec_429 = 0; skipped = 0
    candidates_hq = 0  # paso2 runs with detail:high (blue candidates)
    run_status = "OK"
    leads_at_start = len(leads)
    last_push_ts   = time.time()   # incremental push tracking
    leads_since_push = 0           # push every 5 leads or every 120s
    zip_analyzed = {}; zip_leads = {}  # per-ZIP counters for stats update

    for prop in props:
        if len(leads) >= TARGET_LEADS:
            log(f"✅ Target {TARGET_LEADS} alcanzado!"); break

        folio = prop.get("folio")

        # Skip si ya cacheado — con bypass para modo huracán (re-analiza clean pre-HURRICANE_DATE)
        if folio and folio in folio_cache:
            cached = folio_cache[folio]
            result = cached.get("result")
            if result in ("error", None):
                pass  # siempre reintentar errores
            elif result == "lead":
                skipped += 1; continue  # nunca re-analizar leads
            elif (HURRICANE_DATE and result == "clean"
                  and cached.get("ts", "")[:10] < HURRICANE_DATE):
                pass  # re-analizar limpios anteriores al huracán
            else:
                skipped += 1; continue

        if analyzed >= MAX_ANALYZED:
            log(f"⏹ MAX_ANALYZED {MAX_ANALYZED} alcanzado."); break

        elapsed  = (time.time() - start_time) / 3600
        analyzed += 1
        prop_zip = prop.get("zip", "??")
        zip_analyzed[prop_zip] = zip_analyzed.get(prop_zip, 0) + 1
        addr = prop["address"]
        yr   = prop.get("year_built", "?")
        logp(f"[{analyzed}|{len(leads)}/{TARGET_LEADS}|{elapsed:.1f}h|${STATS['spend_usd']:.4f}] {addr} yr:{yr}\n")

        lat, lng = prop["lat"], prop["lng"]

        # ── Satellite ──
        sat = satellite(lat, lng, 21)
        if not sat:
            logp("  ✗ no sat\n")
            cache_folio(folio_cache, folio, "error", reason="sat_fail")
            errors += 1; sat_fail += 1; consec_ai_fail = 0; time.sleep(2); continue

        # ── Backoff ante rate limits sostenidos (pausa sin abortar) ──
        if consec_ai_fail >= 5:
            logp(f"  ⏸ {consec_ai_fail} AI fails → pausa 90s\n")
            time.sleep(90)
            consec_ai_fail = 0

        # ── PASO 1: chequeo binario rápido (~$0.000452) ──────────────────────
        blue_flag = False  # init; se sobreescribe si paso 1 exitoso
        b1, model1 = ai_call([sat], STEP1_PROMPT, max_tokens=60)

        if b1.get("error") == "hard_limit":
            log(f"🛑 LÍMITE DURO: {b1['last_err']}")
            log(f"   Calls={STATS['api_calls']} | Spend=${STATS['spend_usd']:.4f}")
            run_status = "ABORTED"
            break

        if "error" not in b1:
            # Paso 1 exitoso: evaluar flags
            consec_ai_fail = 0; consec_429 = 0
            blue_flag  = b1.get("blue_or_silver_on_roof", False)
            holes_flag = b1.get("visible_holes_or_missing_sections", False)
            obv_fp     = b1.get("obvious_false_positive", False)
            detr_flag  = b1.get("visible_deterioration", False)
            flag_str   = "+".join(filter(None, [
                "tarp"   if blue_flag  else "",
                "holes"  if holes_flag else "",
                "detr"   if detr_flag  else "",
                "obv_FP" if obv_fp     else "",
            ])) or "clean"
            modl1 = model1.split("/")[-1][:14]
            logp(f"  paso1→ {flag_str:<12} [{modl1}] | {_spend_tag()}\n")

            if not blue_flag and not holes_flag and not detr_flag:
                # Limpio en paso 1 — no gastar el scoring completo
                step1_clean += 1; clean += 1
                cache_folio(folio_cache, folio, "clean", score=1, reason="paso1_clean")
                time.sleep(2); continue

            if blue_flag and obv_fp and not holes_flag:
                # Paso 1 identificó FP obvio (piscina, metal uniforme, carpa a rayas, vecino)
                step1_clean += 1; clean += 1
                logp(f"  ✗ obvio FP (paso1)\n")
                cache_folio(folio_cache, folio, "clean", score=1, reason="paso1_obvious_fp")
                time.sleep(2); continue

            # FP check de segundo nivel: solo cuando blue_flag=True y obv_fp=False
            # Pregunta específicamente por fumigación + vecino — dos FPs que STEP1 suele pasar
            if blue_flag and not obv_fp and not holes_flag:
                fp2, _m2 = ai_call([sat], FP_CHECK_PROMPT, max_tokens=20)
                if "error" not in fp2 and fp2.get("is_fp", False):
                    step1_clean += 1; clean += 1
                    logp(f"  ✗ FP check2 ({fp2.get('reason','?')[:30]})\n")
                    cache_folio(folio_cache, folio, "clean", score=1, reason="paso1_fp_check2")
                    time.sleep(2); continue

            # Tiene flag real → continúa al paso 2
        else:
            # Paso 1 falló → fail-safe: continuar al paso 2
            last_err = b1.get("last_err", "?")
            if "429" in last_err: consec_429 += 1
            errors += 1; ai_fail += 1; consec_ai_fail += 1
            if consec_429 >= 8:
                log(f"🛑 CIRCUIT BREAKER: {consec_429} × 429 consecutivos.")
                break
            logp(f"  paso1 fail [{last_err[:40]}] → scoring igual\n")

        # ── PASO 2: scoring completo ──────────────────────────────────────────
        # Candidatos azules → detail:high (~3× costo imagen, mucho mejor precisión para lona vs piscina)
        # Candidatos con holes solo → detail:low (sin objeto azul visible)
        _b1_ok = "error" not in b1
        _blue_candidate = _b1_ok and b1.get("blue_or_silver_on_roof")
        scoring_prompt = PRESCREEN_PROMPT_BLUE if _blue_candidate else PRESCREEN_PROMPT
        step2_detail   = "high" if _blue_candidate else "low"
        if _blue_candidate:
            candidates_hq += 1
        binary, model_used = ai_call([sat], scoring_prompt, max_tokens=300, detail=step2_detail,
                                     model_list=MODELS_SMART)

        if binary.get("error") == "hard_limit":
            log(f"🛑 LÍMITE DURO: {binary['last_err']}")
            log(f"   Calls={STATS['api_calls']} | Spend=${STATS['spend_usd']:.4f}")
            run_status = "ABORTED"
            break

        if "error" in binary:
            last_err = binary.get("last_err", "?")
            logp(f"  ✗ AI fail paso2 [{last_err}]\n")
            if "429" in last_err:
                consec_429 += 1
            errors += 1; ai_fail += 1; consec_ai_fail += 1
            cache_folio(folio_cache, folio, "error", reason=last_err)
            time.sleep(5)
            if consec_429 >= 8:
                log(f"🛑 CIRCUIT BREAKER: {consec_429} × 429 consecutivos — cuota OR agotada.")
                log(f"   Calls={STATS['api_calls']} | Spend=${STATS['spend_usd']:.4f}")
                run_status = "QUOTA"
                break
            continue

        # Reset contadores — paso 2 exitoso
        consec_ai_fail = 0; consec_429 = 0

        if STATS["last_call_spend"] > SPEND_ALERT_2X:
            log(f"⚠️ ALERTA GASTO: paso2 costó ${STATS['last_call_spend']:.5f} (umbral ${SPEND_ALERT_2X})")

        if not binary.get("is_residential", True):
            not_sfh += 1
            logp("  ✗ not residential\n")
            cache_folio(folio_cache, folio, "not_sfh")
            time.sleep(2); continue

        q    = compute_score(binary, blue_confirmed=blue_flag)
        cond = binary.get("condition", "?")
        tarp = "🔴LONA" if binary.get("tarp_visible") else ""
        miss = "⚠miss"  if binary.get("missing_tiles") or binary.get("broken_tiles") else ""
        modl = model_used.split("/")[-1][:20]
        logp(f"  sat→ score:{q} cond:{cond} {tarp}{miss} [{modl}] | {_spend_tag()}\n")
        logp(f"  → {binary.get('description','')[:90]}\n")

        if q < 5:
            clean += 1
            cache_folio(folio_cache, folio, "clean", score=q)
            time.sleep(3); continue

        # ── Street view — solo para candidatos ──
        has_sv, sv_date = sv_available(lat, lng)
        sv0  = streetview(lat, lng, 0)          if has_sv else None
        sv90 = streetview(lat, lng, 90)         if has_sv else None
        sv45 = streetview(lat, lng, 45, fov=55) if has_sv else None

        detail   = {}
        sv_imgs  = [i for i in [sv0, sv90, sv45] if i]
        if sv_imgs:
            all_imgs   = [i for i in [sat] + sv_imgs if i]
            detail_raw, _ = ai_call(all_imgs, DETAIL_PROMPT, max_tokens=350,
                                     model_list=MODELS_SMART)

            if detail_raw.get("error") == "hard_limit":
                log(f"🛑 LÍMITE DURO (detail): {detail_raw['last_err']}")
                log(f"   Calls={STATS['api_calls']} | Spend=${STATS['spend_usd']:.4f}")
                cache_folio(folio_cache, folio, "error", reason="hard_limit_at_detail")
                break

            if "error" in detail_raw:
                logp(f"  ⚠ detail fail [{detail_raw.get('last_err','?')}] — scoring solo con aerial\n")
                detail_fail += 1
            else:
                detail = detail_raw
                if STATS["last_call_spend"] > SPEND_ALERT_2X:
                    log(f"⚠️ ALERTA GASTO: llamada detail costó ${STATS['last_call_spend']:.5f} (umbral ${SPEND_ALERT_2X})")

        final      = compute_score(binary, detail, blue_confirmed=blue_flag)
        flag       = "🔥LEAD" if final >= 7 else "  meh"
        desc_main   = binary.get("description", "")
        desc_detail = detail.get("damage_description", "")
        full_desc   = (desc_main + " " + desc_detail).strip()

        logp(f"  sv({sv_date})→ score:{final}/10 {flag} {tarp} | {_spend_tag()}\n")
        logp(f"  → {full_desc[:110]}\n")

        if final >= 7:
            logp(f"  ✅ LEAD #{len(leads)+1}\n")
            record = {
                "rank":           len(leads) + 1,
                "address":        addr,
                "lat":            lat, "lng": lng,
                "year_built":     yr,
                "folio":          folio,
                "sv_date":        sv_date,
                "gmaps":          f"https://maps.google.com/?q={lat},{lng}",
                "score_urgencia": final,
                "lona_visible":   binary.get("tarp_visible", False) or detail.get("tarp_confirmed", False),
                "lona_color":     binary.get("tarp_color", "none"),
                "tipo_techo":     binary.get("roof_type", "unknown"),
                "condicion":      binary.get("condition", "?"),
                "broken_tiles":   binary.get("broken_tiles", False),
                "missing_tiles":  binary.get("missing_tiles", False),
                "descripcion":    full_desc,
                "oportunidad_roofing": True,
                "analysis_ts":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "model":          model_used,
            }
            leads.append(record)
            LIVE_FILE.write_text(json.dumps(leads, indent=2))
            append_lead_to_csv(record)
            cache_folio(folio_cache, folio, "lead", score=final)
            zip_leads[prop.get("zip", "??")] = zip_leads.get(prop.get("zip", "??"), 0) + 1
            # Incremental push: cada 5 leads nuevos o cada 2 minutos (rate-limit friendly)
            leads_since_push += 1
            if not DRY_RUN and (leads_since_push >= 5 or (time.time() - last_push_ts) >= 120):
                push_csv_to_github(quiet=True)
                last_push_ts = time.time()
                leads_since_push = 0
        else:
            clean += 1
            cache_folio(folio_cache, folio, "clean", score=final)

        time.sleep(4)

    # ─── Reporte final ────────────────────────────────────────────────────────
    elapsed_m = (time.time() - start_time) / 60
    log(f"\n{'='*65}")
    log(f"FINALIZADO — {elapsed_m:.0f} min | {analyzed} analizadas | {skipped} de cache")
    log(f"  No SFH: {not_sfh} | Techo ok: {clean} | Errores totales: {errors}")
    log(f"  ├─ Paso1 clean: {step1_clean} ({step1_clean/max(analyzed,1)*100:.1f}%) — sin scoring completo")
    log(f"  ├─ Sat fail:    {sat_fail} ({sat_fail/max(analyzed,1)*100:.1f}%)")
    log(f"  ├─ AI fail:     {ai_fail}  ({ai_fail/max(analyzed,1)*100:.1f}%)")
    log(f"  └─ Detail fail: {detail_fail}")
    log(f"  LEADS encontrados: {len(leads)}")
    log(f"  Con lona: {sum(1 for l in leads if l.get('lona_visible'))}")
    maps_cost  = STATS["sat_calls"] * MAPS_SAT_PRICE + STATS["sv_calls"] * MAPS_SV_PRICE
    total_cost = STATS["spend_usd"] + maps_cost
    log(f"  ── GASTO ──────────────────────────────────────────────")
    log(f"  AI (OpenAI/Claude):  ${STATS['spend_usd']:.4f} | {STATS['api_calls']} calls")
    log(f"  Maps API:  ${maps_cost:.4f} ({STATS['sat_calls']} satellite × $0.002 + {STATS['sv_calls']} streetview × $0.007)")
    log(f"  TOTAL ESTIMADO:      ${total_cost:.4f}")
    log(f"  Costo/prop:          ${total_cost/max(analyzed,1):.5f} promedio")
    dry_tag = " [DRY RUN — sin push a GitHub]" if DRY_RUN else ""
    log(f"\nResultados: {LIVE_FILE}{dry_tag}")
    new_leads = leads[leads_at_start:]
    if new_leads:
        log(f"TOP LEADS (nuevos en esta corrida — {len(new_leads)} total):")
        for l in sorted(new_leads, key=lambda x: x.get("score_urgencia", 0), reverse=True)[:10]:
            lona = " 🔴LONA" if l.get("lona_visible") else ""
            log(f"  [{l['score_urgencia']}/10] yr:{l.get('year_built','?')} {l['address']}{lona}")
    if DRY_RUN and new_leads:
        rate = len(new_leads) / max(analyzed - skipped, 1) * 100
        log(f"\n{'='*65}")
        log(f"DRY RUN SUMMARY")
        log(f"  Casas analizadas (netas):   {analyzed - skipped}")
        log(f"  Leads encontrados:          {len(new_leads)}")
        log(f"  TASA:                       {rate:.2f}%")
        log(f"  Gasto total:                ${total_cost:.4f}")
        log(f"  Muestra de leads (verif visual):")
        for l in sorted(new_leads, key=lambda x: x.get("score_urgencia", 0), reverse=True)[:10]:
            log(f"    [{l['score_urgencia']}/10] {l['address']}  →  {l.get('gmaps','')}")

    # ─── Actualizar zip_stats + avanzar offset GIS para próxima corrida ─────────
    today = datetime.now().strftime("%Y-%m-%d")
    for zc in run_zips:
        if zc not in zip_stats:
            zip_stats[zc] = {"analyzed": 0, "leads": 0, "gis_offset": 0}
        zip_stats[zc]["analyzed"]  = zip_stats[zc].get("analyzed", 0) + zip_analyzed.get(zc, 0)
        zip_stats[zc]["leads"]     = zip_stats[zc].get("leads", 0)    + zip_leads.get(zc, 0)
        zip_stats[zc]["last_run"]  = today
        zip_stats[zc]["inventory"] = ALL_ZIPS.get(zc, 0)
        if not HURRICANE_ZIPS:
            zip_stats[zc]["gis_offset"] = zip_stats[zc].get("gis_offset", 0) + 500
    save_zip_stats(zip_stats)
    log(f"ZIP stats guardados: {len(zip_stats)} ZIPs con historial")

    write_run_history(
        run_zips=run_zips, analyzed=analyzed,
        candidates_hq=candidates_hq,
        leads_new=len(leads) - leads_at_start,
        leads_total=len(leads),
        gasto=STATS["spend_usd"], sat_fail=sat_fail, ai_fail=ai_fail,
        errors=errors, status=run_status
    )
    if not DRY_RUN:
        push_csv_to_github()
        backup_folio_cache_to_github()
    else:
        log("DRY RUN: push a GitHub omitido.")

def write_run_history(run_zips, analyzed, candidates_hq, leads_new, leads_total,
                      gasto, sat_fail, ai_fail, errors, status):
    """Append one row to local run_history.csv then push to GitHub data/run_history.csv."""
    fecha = datetime.now().strftime("%Y-%m-%d")
    row = {
        "fecha":                 fecha,
        "zips_usados":           "|".join(run_zips),
        "casas_analizadas":      analyzed,
        "candidatos_detail_high": candidates_hq,
        "leads_nuevos":          leads_new,
        "leads_totales":         leads_total,
        "gasto_usd":             f"{gasto:.4f}",
        "sat_fail":              sat_fail,
        "ai_fail":               ai_fail,
        "errores":               errors,
        "status":                status,
    }
    write_header = not RUN_HISTORY_FILE.exists() or RUN_HISTORY_FILE.stat().st_size == 0
    with open(RUN_HISTORY_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=RUN_HISTORY_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    log(f"Run history guardado: {row}")

    # Push to GitHub
    if not GITHUB_TOKEN:
        log("⚠️ run_history push skipped (no GITHUB_TOKEN)")
        return
    try:
        content = base64.b64encode(RUN_HISTORY_FILE.read_bytes()).decode()
        api_path = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/run_history.csv"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        existing = requests.get(api_path, headers=headers, timeout=15).json()
        sha = existing.get("sha", "")
        payload = {"message": f"auto: run history {fecha} ({status})", "content": content}
        if sha:
            payload["sha"] = sha
        r = requests.put(api_path, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log(f"✅ run_history pushed to GitHub")
        else:
            log(f"⚠️ run_history push failed: {r.json().get('message','?')[:60]}")
    except Exception as e:
        log(f"⚠️ run_history push error: {e}")

def push_csv_to_github(quiet=False):
    """Push leads_para_ventas.csv a GitHub mergeando con el CSV existente (acumulativo por folio).
    El CSV en GitHub siempre contiene TODOS los leads históricos — nunca se sobreescribe limpio."""
    if not GITHUB_TOKEN:
        if not quiet:
            log("⚠️ GitHub push skipped (no GITHUB_TOKEN)")
        return
    try:
        import io as _io
        api_path = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/leads_para_ventas.csv"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

        # 1. Leer CSV existente en GitHub → dict por folio
        gh_rows = {}
        gh_sha = ""
        existing_resp = requests.get(api_path, headers=headers, timeout=15)
        if existing_resp.status_code == 200:
            gh_meta = existing_resp.json()
            gh_sha = gh_meta.get("sha", "")
            gh_text = base64.b64decode(gh_meta["content"]).decode("utf-8-sig", errors="replace")
            for row in csv.DictReader(gh_text.splitlines()):
                folio = (row.get("Folio") or "").strip()
                if folio:
                    gh_rows[folio] = row

        # 2. Leer CSV local → dict por folio
        local_rows = {}
        if LEADS_CSV_FILE.exists() and LEADS_CSV_FILE.stat().st_size > 0:
            with open(LEADS_CSV_FILE, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    folio = (row.get("Folio") or "").strip()
                    if folio:
                        local_rows[folio] = row

        # 3. Merge inteligente:
        #    - Leads nuevos (solo en local) → se agregan completos
        #    - Leads existentes (en ambos) → campos del scanner desde local,
        #      campos operacionales (Estado, Contactado, Daño, Notas) → GitHub gana
        #      si tiene valor no vacío (preserva correcciones manuales del equipo)
        OPERATIONAL_FIELDS = {
            "Estado", "Contactado (sí/no)",
            "Daño confirmado (sí/no/no visible)", "Notas del setter"
        }
        merged = {}
        all_folios = set(gh_rows.keys()) | set(local_rows.keys())
        for folio in all_folios:
            if folio in local_rows and folio not in gh_rows:
                merged[folio] = local_rows[folio]
            elif folio in gh_rows and folio not in local_rows:
                merged[folio] = gh_rows[folio]
            else:
                row = dict(local_rows[folio])
                for field in OPERATIONAL_FIELDS:
                    gh_val = (gh_rows[folio].get(field) or "").strip()
                    if gh_val:
                        row[field] = gh_val
                merged[folio] = row
        if not merged:
            if not quiet:
                log("⚠️ CSV push skipped (sin leads)")
            return

        # 4. Escribir merged de vuelta al archivo local (estado local = estado GitHub)
        buf = _io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in merged.values():
            writer.writerow(row)
        merged_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
        LEADS_CSV_FILE.write_bytes(merged_bytes)

        # 5. Push a GitHub
        content = base64.b64encode(merged_bytes).decode()
        fecha = datetime.now().strftime("%Y-%m-%d")
        payload = {"message": f"auto: scanner run {fecha} ({len(merged)} leads)", "content": content}
        if gh_sha:
            payload["sha"] = gh_sha
        r = requests.put(api_path, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            if not quiet:
                log(f"✅ CSV pushed to GitHub ({len(merged)} leads totales, {len(local_rows)} de esta corrida)")
            else:
                logp(f"  → CSV push incremental OK ({len(merged)} leads totales)\n")
        else:
            log(f"⚠️ GitHub push failed: HTTP {r.status_code} — {r.json().get('message','?')[:60]}")
    except Exception as e:
        log(f"⚠️ GitHub push error: {e}")

if __name__ == "__main__":
    main()
