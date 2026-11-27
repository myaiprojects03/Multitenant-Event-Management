#!/bin/sh
# Container entry point: migrate, then serve. Hosts may set $PORT; the default is 8000.
set -e

# No REDIS_URL (single-service hosting, e.g. Render free): run a local Redis. It holds only the job queue, the
# response cache and rate-limit counters, so losing it on a restart is fine.
if [ -z "$REDIS_URL" ]; then
  redis-server --daemonize yes --bind 127.0.0.1 --save "" --appendonly no --dir /tmp
fi

# Migrations are idempotent, so running them on every boot is safe. sync_roles re-applies
# the app role passwords in case the secrets changed since the roles were created.
alembic upgrade head
python -m app.sync_roles
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*'
