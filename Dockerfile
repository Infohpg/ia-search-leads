FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner_overnight.py .
# /data is mounted as persistent volume — holds analyzed_folios.json, overnight_leads.json, leads_para_ventas.csv
RUN mkdir -p /data && ln -s /data /app/scan_results
CMD ["python3", "scanner_overnight.py"]
