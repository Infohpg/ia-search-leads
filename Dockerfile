FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner_overnight.py .
COPY startup.sh .
RUN chmod +x /app/startup.sh
# /data is mounted as persistent volume — holds analyzed_folios.json, leads_para_ventas.csv, etc.
RUN mkdir -p /data && ln -s /data /app/scan_results
# Install cron for daily scheduled runs
RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*
# Cron schedule set dynamically in startup.sh (env var CRON_SCHEDULE, default: 0 10 * * *)
EXPOSE 8080
# startup.sh: HTTP healthcheck immediately → scan if not done today → cron -f
CMD ["/bin/bash", "/app/startup.sh"]
