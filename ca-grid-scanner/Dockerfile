FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests

COPY ca_grid_scanner.py .

# scan_results se monta como volumen en Sliplane para persistencia
RUN mkdir -p scan_results

CMD ["python3", "ca_grid_scanner.py"]
