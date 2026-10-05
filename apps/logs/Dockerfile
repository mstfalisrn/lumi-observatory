# LUMI — canlı günlük servisi (LLM + Jev kararları, salt okunur)
# Tek bağımlılık: psycopg. Sadece SELECT çalıştırır.
FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "psycopg[binary]==3.2.3" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin lumilogs

WORKDIR /app
COPY apps/logs/app.py /app/app.py
# the shared data layer + redaction helper (same files the API image carries)
COPY packages/observability/__init__.py /app/packages/observability/__init__.py
COPY packages/observability/live_log.py /app/packages/observability/live_log.py
COPY packages/observability/security.py /app/packages/observability/security.py
ENV PYTHONPATH=/app/packages

# run unprivileged; compose additionally drops caps and mounts the rootfs read-only
USER 10001:10001

EXPOSE 8000
CMD ["python", "app.py"]
