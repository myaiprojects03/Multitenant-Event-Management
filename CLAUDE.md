# Multi-Tenant Event AI Platform

Solo portfolio project. Spec: `docs/spec.txt`. **Build plan and locked decisions: `docs/PLAN.md`. Follow it phase by phase; ignore its deadlines (go as fast as possible).**

## Local setup facts
- Windows; Python 3.11 venv at `backend/.venv` (system default Python is 3.14, so don't use it).
- A native PostgreSQL 18 runs on 5432, so the Docker Postgres is mapped to **5433**.
- Config is read from the repo-root `.env` (copy of `.env.example`). Never commit `.env`.
- Docker CLI may not be on PATH in a fresh shell: prefix with `$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`.
- Gemini models (free tier, this key): 2.5 models are unavailable to new users. Fast = `gemini-3.5-flash-lite`. Smart = `gemini-3.5-flash` (3.7/3.8-flash often return 503 "high demand"). Phase 2 gateway needs a fallback chain.
- PyPI downloads are flaky on this network: Dockerfile uses long pip timeouts + a cache mount.

## Commands
- Services: `docker compose up -d db redis` (all: `docker compose up --build -d`)
- Migrate: `cd backend; .\.venv\Scripts\alembic upgrade head` (runs as the owner role)
- Seed (resets all data, anchored to today): `cd backend; .\.venv\Scripts\python -m app.seed`
- API locally: `cd backend; .\.venv\Scripts\uvicorn app.main:app --reload`
- Tests (DB tests need migrate + seed): `cd backend; .\.venv\Scripts\pytest`
- Lint/format: `cd backend; .\.venv\Scripts\ruff check . ; .\.venv\Scripts\ruff format .`

## Architecture notes
- DB roles: owner (migrations/seed), `app_rw` (API; RLS applies), `app_readonly` (NL-to-SQL; SELECT on reporting views only, read-only, 5s timeout).
- Request scope: `app/core/context.py` (`RequestContext`, `apply_context`, `scoped_session`); `app/core/security.py` (JWT, `get_ctx`, `get_db`, `require_roles`). Background jobs use `RequestContext.system(...)`; `system` is never put in a JWT.
- RLS policies are declared as data in `migrations/versions/0002_security.py` (`EVENT_TABLES` / `OTHER_TABLES`). A new table needs an entry there, or the isolation tests fail.
- Reporting views (`0003_reporting_views.py`) run as owner and filter on `app_tenant()/app_event()/app_is_staff()` themselves.
- Multi-statement SQL in migrations goes through `migrations/sqlutil.exec_script` (asyncpg runs one statement per call).
- Seed data: `app/seed/catalog.py` (personas, planted patterns) + `generator.py` (pure, deterministic). "Now" = 08:00 UTC on the anchor date; main events run anchor-2 .. anchor, so day 3 is upcoming.

## Non-negotiables
- Every table has `tenant_id` + `event_id` and RLS; tenant/event/role/user scope comes from the authenticated `RequestContext`, never from the LLM.
- NL-to-SQL runs only through the sqlglot guard on approved views as a read-only role.
- No external action (email, schedule change, bulk) executes without HITL approval.
- Only synthetic data. Email/SMS/webhooks are simulated.
