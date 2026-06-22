#!/bin/bash
# CA Roof Scanner (GIS LA County) — cron diario 6am Venezuela (10:00 UTC)
set -a
source /Users/luisfeliperodriguez/clientes/_credenciales/home-pro-guides/.env
set +a

export MAX_ANALYZED=6000
export MAX_AI_SPEND_USD=5.00
export DRY_RUN=0
# bbox: South LA / Inglewood / Compton / Hawthorne (257k+ casas en total)
export SCANNER_BBOX="-118.500,33.860,-118.100,34.050"
# Cap de descarga GIS: 30000 = ~3 min, pool de 5 días antes de reusar el ciclo
export GIS_MAX_RECORDS=30000

cd /Users/luisfeliperodriguez/clientes/home-pro-guides/trabajos/scripts/ca-grid-scanner
LOG="scan_results/ca_scan_$(date +%Y%m%d).log"
echo "CA SCAN INICIADO — $(date)" >> "$LOG"
/opt/homebrew/bin/python3 ca_gis_scanner.py >> "$LOG" 2>&1
echo "CA SCAN FINALIZADO — exit $? — $(date)" >> "$LOG"
