# LUMI — combined API and UI image (multi-stage)
# Stage 1: Web build (node:22-alpine pinned)
ARG PYTHON_IMAGE
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS web-build
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json /web/
RUN npm ci --no-audit --no-fund
COPY apps/web/ /web/
ENV VITE_API_BASE=/api VITE_SSE_BASE=/api/v1/events/stream
RUN npm run build

# Stage 2: Python API and static UI (python:3.12-slim pinned)
FROM ${PYTHON_IMAGE}

RUN groupadd -g 10001 lumi && useradd -u 10001 -g lumi -s /usr/sbin/nologin lumi

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/lumi

COPY packages/requirements-api.txt /srv/lumi/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY packages/ /srv/lumi/
COPY apps/api/ apps/api/
COPY migrations/ migrations/

COPY --from=web-build /web/dist/ /srv/lumi/apps/web/dist/

USER lumi
EXPOSE 8000
CMD ["sh", "-c", "uvicorn apps.api.app:app --host 0.0.0.0 --port 8000 --workers 1"]