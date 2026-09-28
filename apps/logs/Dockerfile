# LUMI — canlı günlük servisi (LLM + Jev kararları, salt okunur)
# Tek bağımlılık: psycopg. Sadece SELECT çalıştırır.
FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"

WORKDIR /app
COPY apps/logs/app.py /app/app.py

EXPOSE 8000
CMD ["python", "app.py"]
