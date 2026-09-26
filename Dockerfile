FROM python:3.12-slim

# Logs appear immediately in Railway instead of being buffered
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

# SQLite lives on a Railway Volume mounted at /data (set DB_PATH=/data/leads.db)
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "bot.main"]
