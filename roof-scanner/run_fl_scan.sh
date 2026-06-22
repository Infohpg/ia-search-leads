#!/bin/bash
# FL Roof Scanner — cron diario 6am Venezuela (10:00 UTC)
set -a
source /Users/luisfeliperodriguez/clientes/_credenciales/home-pro-guides/.env
set +a

export STATE_TAG=FL
export SCANNER_MAX=6000
export MAX_HOUSES_DAY=6000
export HOT_LEADS_TARGET=9999
export TOTAL_LEADS_CAP=9999
export MAX_SPEND_USD=5.00
export MAX_API_CALLS_LIMIT=8000
export GIS_BATCH_SIZE=750
export ZIP_N_TOP=8
export ZIP_N_NEW=4
export DRY_RUN=0
export VERIFY_MODE=0
export COMPARISON_MODE=0
# Modelo: OpenRouter → openai/gpt-4o-mini (pago, mismo que CA). Sin free fallbacks como primario.
export STEP1_PROVIDER=openrouter
export STEP1_MODEL=openai/gpt-4o-mini
export SMART_PROVIDER=openrouter
export SMART_MODEL=openai/gpt-4o-mini

cd /Users/luisfeliperodriguez/clientes/home-pro-guides/trabajos/scripts/roof-scanner
LOG="scan_results/fl_scan_$(date +%Y%m%d).log"
echo "FL SCAN INICIADO — $(date)" >> "$LOG"
/opt/homebrew/bin/python3 scanner_overnight.py >> "$LOG" 2>&1
echo "FL SCAN FINALIZADO — exit $? — $(date)" >> "$LOG"
