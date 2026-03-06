# Dockerfile — NeoLife API (beta, sin disk)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY neobot_main.py .
COPY neobot_db.py .
COPY neobot_calendar.py .
COPY crm_dashboard.html .

# /tmp es efímero pero suficiente para testing
# Los datos reales van a Google Sheets (persisten siempre)
ENV DB_PATH=/tmp/neolife_crm.db

EXPOSE 8000

CMD ["uvicorn", "neobot_main:app", "--host", "0.0.0.0", "--port", "8000"]
