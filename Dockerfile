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
# Daily cron: 10:00 UTC = 06:00 ET (EDT) / 05:00 ET (EST)
RUN echo "0 10 * * * cd /app && python3 scanner_overnight.py >> /data/cron.log 2>&1" | crontab -
EXPOSE 8080
# startup.sh: HTTP healthcheck immediately → scan if not done today → cron -f
CMD ["/bin/bash", "/app/startup.sh"]
