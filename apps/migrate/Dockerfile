# LUMI — one-shot migration service (the API does not run migrations; no web build)
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

RUN groupadd -g 10001 lumi && useradd -u 10001 -g lumi -s /usr/sbin/nologin lumi

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/lumi

COPY packages/requirements-api.txt /srv/lumi/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY packages/ /srv/lumi/
COPY migrations/ migrations/

USER lumi
CMD ["sh", "-c", "alembic -c migrations/alembic.ini upgrade head"]
