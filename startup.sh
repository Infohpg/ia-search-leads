#!/bin/bash
# startup.sh — HPG IA Roof Scanner container entrypoint
# Arranca HTTP healthcheck inmediatamente, corre el scanner si es primera corrida del día,
# luego deja el cron daemon como proceso principal para corridas diarias.

set -e

# 1. HTTP healthcheck en :8080 — arranca ANTES del scan para que Sliplane no timeout
cd /app
python3 -m http.server 8080 &

# 2. Exportar env vars a archivo para que cron pueda usarlas
#    (cron corre en ambiente limpio, NO hereda las env vars de Docker)
python3 -c "
import os
lines = []
for k, v in os.environ.items():
    if k.startswith('_'):
        continue
    escaped = v.replace(\"'\", \"'\\\\'' \").rstrip()
    lines.append(f\"export {k}='{escaped}'\")
with open('/tmp/docker_env.sh', 'w') as f:
    f.write('\n'.join(lines) + '\n')
"
chmod 600 /tmp/docker_env.sh

# 3. ¿Correr scan ahora?
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
    echo "$TS Corrida de hoy ya registrada — saltando startup scan." >> /data/cron.log
fi

if [ "$SHOULD_RUN" = "1" ]; then
    python3 /app/scanner_overnight.py >> /data/cron.log 2>&1
    echo "$TS Startup scan finalizado." >> /data/cron.log
fi

# 4. Instalar crontab con env vars — schedule configurable via CRON_SCHEDULE
#    Default: 0 10 * * * (10:00 UTC = 06:00 ET)
SCHEDULE="${CRON_SCHEDULE:-0 10 * * *}"
echo "$TS Instalando cron: '$SCHEDULE'" >> /data/cron.log
echo "$SCHEDULE . /tmp/docker_env.sh && cd /app && python3 /app/scanner_overnight.py >> /data/cron.log 2>&1" | crontab -

# 5. Tail cron.log a stdout — output visible en logs de Sliplane
touch /data/cron.log
tail -F /data/cron.log &

# 6. Cron daemon como proceso principal
cron -f
