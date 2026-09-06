FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

RUN groupadd -g 10001 lumi && useradd -u 10001 -g lumi -s /usr/sbin/nologin lumi
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/lumi
COPY packages/requirements-scheduler.txt /srv/lumi/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY packages/ /srv/lumi/
COPY apps/scheduler/ /srv/lumi/apps/scheduler/
USER lumi
EXPOSE 8002
CMD ["uvicorn", "apps.scheduler.scheduler:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]