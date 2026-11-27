# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    EMBEDDING_CACHE_DIR=/models/fastembed

WORKDIR /app

COPY requirements.txt .
# Cache mount keeps downloaded wheels between builds, so a flaky connection doesn't restart from zero.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Bake the embedding model into the image: a host that sleeps and wakes (Render free) would
# otherwise download ~130 MB on every cold start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models/fastembed')"

COPY . .

EXPOSE 8000
# Migrations are idempotent, so running them on every boot is safe; hosts set $PORT (Render: 10000).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
