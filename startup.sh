#!/bin/bash
# startup.sh — HPG IA Roof Scanner container entrypoint
# Arranca HTTP healthcheck inmediatamente, corre el scanner si es primera corrida del día,
# luego deja el cron daemon como proceso principal para corridas diarias.

set -e

# 1. HTTP healthcheck en :8080 — arranca ANTES del scan para que Sliplane no timeout
cd /app
python3 -m http.server 8080 &

# 2. ¿Correr scan ahora?
TODAY=$(date +%Y-%m-%d)
HISTORY=/data/run_history.csv
TS="[$(date '+%Y-%m-%d %H:%M:%S UTC')]"

SHOULD_RUN=0
if [ "${FORCE_STARTUP_SCAN:-0}" = "1" ]; then
    echo "$TS Startup scan FORZADO (FORCE_STARTUP_SCAN=1)" >> /data/cron.log
    SHOULD_RUN=1
elif [ ! -f "$HISTORY" ] || ! grep -q "^${TODAY}" "$HISTORY" 2>/dev/null; then
    echo "$TS Startup scan: no hay corrida de hoy en run_history → iniciando" >> /data/cron.log
    SHOULD_RUN=1
else
    echo "$TS Corrida de hoy ya registrada — saltando startup scan. Próximo cron: 10:00 UTC" >> /data/cron.log
fi

if [ "$SHOULD_RUN" = "1" ]; then
    python3 /app/scanner_overnight.py >> /data/cron.log 2>&1
    echo "$TS Startup scan finalizado." >> /data/cron.log
fi

# 3. Cron daemon — corrida diaria 10:00 UTC = 6:00 AM ET
cron -f
