FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner_overnight.py .
# /data is mounted as persistent volume — holds analyzed_folios.json, overnight_leads.json, leads_para_ventas.csv
RUN mkdir -p /data && ln -s /data /app/scan_results
# Install cron
RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*
# Run daily at 10:00 UTC = 06:00 ET (EDT) / 05:00 ET (EST)
RUN echo "0 10 * * * cd /app && python3 scanner_overnight.py >> /data/cron.log 2>&1" | crontab -
# Run once at container start to seed data, then hand off to cron
CMD cron -f
