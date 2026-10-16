# Single-container image: FastAPI app + PostgreSQL 16 + pgvector; all state on the /data volume.
FROM ubuntu:24.04

# USE_PGDG=1 adds the official PostgreSQL apt repo (pgvector 0.8, which has iterative HNSW
# scans for filtered searches). With 0, Ubuntu's own packages are used (pgvector 0.6).
ARG USE_PGDG=1
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PG_MAJOR=16 \
    DATA_DIR=/data \
    FASTEMBED_CACHE_PATH=/opt/models \
    PATH=/opt/venv/bin:$PATH

# Postgres is installed at build time so restarts never touch apt. No default cluster is
# created: ours is initialised on the /data volume by scripts/ensure_local_db.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl postgresql-common python3 python3-venv procps \
 && sed -ri 's/^#?\s*create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf \
 && if [ "$USE_PGDG" = "1" ]; then /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y; fi \
 && apt-get install -y --no-install-recommends postgresql-${PG_MAJOR} postgresql-${PG_MAJOR}-pgvector \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && pip install --upgrade pip && pip install -r requirements.txt

# Bake the embedding model into the image so boots never download it (PRELOAD_MODEL=0 skips).
ARG PRELOAD_MODEL=1
RUN if [ "$PRELOAD_MODEL" = "1" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/models')"; \
    fi

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1
# Exactly one uvicorn process: it owns the local Postgres server and the worker pool.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 20"]
