# Multi-Tenant Event AI Platform

Solo portfolio project. Spec: `docs/spec.txt`. **Build plan and locked decisions: `docs/PLAN.md`. Follow it phase by phase; ignore its deadlines (go as fast as possible).**

## Local setup facts
- Windows; Python 3.11 venv at `backend/.venv` (system default Python is 3.14, so don't use it).
- A native PostgreSQL 18 runs on 5432, so the Docker Postgres is mapped to **5433**.
- Config is read from the repo-root `.env` (copy of `.env.example`). Never commit `.env`.
- Docker Desktop is a per-user install: `C:\Users\abcd\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`; CLI in `...\DockerDesktop\resources\bin` (prepend to PATH in a fresh shell). It does not auto-start: if DB tests are all skipped, Docker is down.
- Gemini models (free tier, this key): 2.5 models are unavailable to new users. Fast = `gemini-3.5-flash-lite` (fallback 3.1-flash-lite). Smart = `gemini-3.5-flash` (fallback 3.6-flash); 3.7/3.8-flash often return 503 "high demand". `thinking_level="low"` roughly halves Flash latency.
- Re-run the seed after the date changes, or "yesterday"-style analytics questions point at the wrong day.
- PyPI downloads are flaky on this network: Dockerfile uses long pip timeouts + a cache mount.

## Commands
- Services: `docker compose up -d db redis` (all: `docker compose up --build -d`)
- Migrate: `cd backend; .\.venv\Scripts\alembic upgrade head` (runs as the owner role)
- Seed (resets all data, anchored to today, then re-indexes for the assistant): `cd backend; .\.venv\Scripts\python -m app.seed` (`--no-index` to skip)
- Re-index only: `cd backend; .\.venv\Scripts\python -m app.ingest` (only changed cards are re-embedded)
- API locally: `cd backend; .\.venv\Scripts\uvicorn app.main:app --reload`
- Tests (DB tests need migrate + seed): `cd backend; .\.venv\Scripts\pytest`
- Live Gemini tests (use quota): `$env:RUN_LIVE_LLM = "1"; .\.venv\Scripts\pytest tests/test_llm_live.py -s`
- Lint/format: `cd backend; .\.venv\Scripts\ruff check . ; .\.venv\Scripts\ruff format .`
- Evals (reseed first): `cd backend; .\.venv\Scripts\python -m evals.run` (full, ~20 min, uses quota; writes `evals/reports/latest.md` + `.json`), `--sample 5` (quick, writes `sample.md`), `--no-llm --strict` (deterministic part; CI runs it), `--suites rag,nl_sql`.
- Console: `cd frontend; npm run dev` (port 3000, proxies `/api/*` to `API_URL`, default localhost:8000); check with `npm run build` (type-checks). In compose it is the `web` service.

## Architecture notes
- DB roles: owner (migrations/seed), `app_rw` (API; RLS applies), `app_readonly` (NL-to-SQL; SELECT on reporting views only, read-only, 5s timeout).
- Request scope: `app/core/context.py` (`RequestContext`, `apply_context`, `scoped_session`); `app/core/security.py` (JWT, `get_ctx`, `get_db`, `require_roles`). Background jobs use `RequestContext.system(...)`; `system` is never put in a JWT.
- RLS policies are declared as data in `migrations/versions/0002_security.py` (`EVENT_TABLES` / `OTHER_TABLES`). A new table needs an entry there, or the isolation tests fail.
- Reporting views (`0003_reporting_views.py`) run as owner and filter on `app_tenant()/app_event()/app_is_staff()` themselves.
- Multi-statement SQL in migrations goes through `migrations/sqlutil.exec_script` (asyncpg runs one statement per call).
- AI plumbing (`app/core`): `llm.py` (`get_llm().generate(..., ctx=, route_type=, tier=, schema=)`; the only Gemini entry point), `embeddings.py` (`get_embedder()`, passage vs query embeddings), `cache.py` (`ResponseCache`: key includes role, exhibitor_id, optional user_id and the event version; `invalidate_event` on content change), `ratelimit.py` (`rate_limited("llm")` dependency), `telemetry.py` (`record_usage`, request middleware, `redact`).
- Assistant (`app/modules/assistant`): `cards.py` (entity → text card, visibility copied from the source row), `indexer.py` (hash-based upsert; `reindex_entity` also refreshes dependent cards, e.g. a session's speakers), `retrieval.py` (`hybrid_search`, `LOOKUPS`), `tools.py` (bookmark / list bookmarks / filter exhibitors; role checks in code), `service.py` (router → lookup | grounded answer | tool | refusal; cache). Staff are never refused by the router; RLS is the real boundary.
- Webhooks (`app/api/webhooks.py`): HMAC over `"{ts}.{body}"`, 5-min replay window, delivery-id dedupe, change applied under a `system` ctx for the payload's event, then `invalidate_event` + `reindex_entity_job` (arq, `app/workers/main.py`). Ali's default login event is the *Forum*; pass `event_id` to target the summit.
- SQLAlchemy `Row` has built-in attributes (`t`, `count`, `index`...): don't alias SQL columns to those names.
- Matchmaking (`app/modules/matchmaking`): `vectors.py` (profile_vectors, built by `app.ingest`/seed), `scoring.py` (pure: overlap, needs↔offers, `Feedback`, `diversify`), `service.py` (per-type candidate SQL + scoring + reasons). Consent-crossing steps live in SECURITY DEFINER SQL (`0004_matchmaking.py`): `exhibitor_prospect_matches`, `peer_session_overlap`, `request_meeting_with_prospect`, `respond_meeting` (creates the lead), `badge_scan`. Each re-checks `app_role()/app_tenant()/app_event()` itself: keep it that way.
- Live tests must not persist side effects (impressions, bookmarks): run them in a rolled-back transaction, or later order-sensitive tests break.
- Append-only log tables (`engagement_logs`, `audit_logs`, `llm_usage_logs`) set `__table_kwargs__ = _APPEND_ONLY` (no implicit RETURNING): Postgres checks RETURNING rows against the read policy, so attendees could not insert otherwise. Any new write-but-not-read table needs the same.
- Seed data: `app/seed/catalog.py` (personas, planted patterns) + `generator.py` (pure, deterministic for a given seed time). Main events run anchor-2 .. anchor+1: two past days (spike = yesterday), a short program today (check-ins only for sessions that ended before seeding), and tomorrow, which holds the overbooked FHIR workshop (pinned to Workshop Lab 1).
- BI copilot (`app/modules/analytics`): `catalog.py` (approved views + columns; the only schema the model sees), `guard.py` (`guard_sql`, deny by default; sqlglot quirk: AND/OR are `Func` nodes, skip `exp.Connector`), `executor.py` (`run_guarded` as app_readonly via `exec_driver_sql` so `:MI` in to_char isn't a bind param), `nl_sql.py` (plan → guard → run → one repair → summary), `anomalies.py` (rule/stat flags). A new reporting view needs an entry in `catalog.VIEWS` and a GRANT to app_readonly.
- Workflows (`app/modules/workflows`): `states.py` (transition map; every status change goes through `check_transition`), `policies.py` (`load_policy`, `approval_chain`, `flag_reasons`), `agents.py` (Lead Qualification + Operations agents; scores/rooms in code, LLM only writes text; `apply_edits` text-only), `engine.py` (trigger → run as owner → approvals → `_materialize` → `deliver`; bookkeeping via `as_system`), `sender.py` (simulated email/platform/publish, provider-side idempotency, failure injection via params `simulate`). `outbound_actions` RLS (0005) only admits rows for fully approved workflows. Worker: `run_workflow_job`, `deliver_workflow_job` (reschedules itself for backoff), cron `sweep_workflows` (uses context-free `workflow_due_work()`).
- `DbDep` uses `Depends(get_db, scope="function")`: the request transaction commits before the response is sent, so BackgroundTasks (job enqueues) see committed rows. Keep it that way.
- Tests that start workflows must not enqueue real jobs (monkeypatch `jobs.enqueue`) and use Redis db 15 for the simulated provider.

- Console (`frontend/`): Next.js App Router, all pages client components under `app/(console)/`; `lib/api.ts` (fetch + token), `lib/session.tsx` (per-tab sessionStorage), `lib/nav.ts` (nav per role + route guard), `components/Workflow.tsx` (workflow drawer: draft editors, delivery, history). Design rules: one accent (teal), neutral surfaces, no gradients/glows/emojis/nested cards/rainbow accents; state shown as text badges. Browser tests mutate demo data (room moves, sends): reseed before `pytest`.
- LLM gateway skips a model for `llm_busy_cooldown_s` after it exhausts its attempts on 429/503/504/timeout (per-process; `get_llm()` is cached).

- Evals (`backend/evals`): golden YAML in `golden/` (facts the seed generates are looked up with owner SQL `vars`, so sets survive a reseed), `harness.py` (in-process ASGI client, demo logins, owner SQL, request ids `ev<run>-...` to pull tokens from llm_usage_logs), `suites/*` (rag, rbac, nl_sql, sql_safety, matching, workflows), `report.py`. Matching and workflow suites run in rolled-back transactions; only `latest.*` reports are committed. A new planted secret or role rule belongs in `golden/rbac.yaml`.
- Gemini free-tier smart models (3.5/3.6-flash) can be out of daily quota (429): the gateway then answers with flash-lite, which the eval report shows under "Models used".

- RLS is ENABLED but not FORCED (migration 0006): the owner (migrations, seed, SECURITY DEFINER functions) skips it as table owner; app_rw/app_readonly are never owners and have NOBYPASSRLS. Locally the Docker owner is a superuser, so owner-vs-RLS bugs only show on managed Postgres: rehearse with a non-superuser owner that has CREATEROLE. Don't put SUPERUSER/BYPASSRLS clauses in ALTER ROLE (superuser-only even to turn off). `assert_rls_applies` (app/core/db.py) guards API startup (APP_ENV=production) and `app.ingest`: indexing relies on RLS to scope each event, so never run it on the owner URL.
- Hosting (no card): API and console are two Vercel projects (API root `backend`, FastAPI detected at `app/main.py`, `backend/vercel.json`; Vercel reads deps from `pyproject.toml`, which must match `requirements.txt` (test_config checks)); Neon Postgres; Upstash Redis via Vercel Storage (`KV_URL` is accepted as `REDIS_URL`). `VERCEL` turns on `serverless`: `jobs.enqueue` runs the job inline (after its delay; failures logged), main.py's middleware runs `sweep_workflows` after a response at most every 30 min, `app/core/loops.py` rebuilds DB/Redis/Gemini clients if the event loop changes, embeddings cache in /tmp. Render (Blueprint and web service) and HF Docker Spaces asked for a card or PRO. `.github/workflows/reseed-demo.yml` migrates + reseeds Neon nightly (secrets DATABASE_OWNER_URL, APP_SECRET). The Docker image (`start.sh`: local Redis if `REDIS_URL` unset, migrate, `app.sync_roles`, uvicorn) still works for container hosts. Config: `DATABASE_OWNER_URL` expands into all three DB URLs (strips Neon `-pooler`, adds `ssl=require`); `APP_SECRET` derives role passwords + JWT/webhook secrets not set explicitly; production refuses the dev JWT secret.

## Non-negotiables
- Every table has `tenant_id` + `event_id` and RLS; tenant/event/role/user scope comes from the authenticated `RequestContext`, never from the LLM.
- NL-to-SQL runs only through the sqlglot guard on approved views as a read-only role.
- No external action (email, schedule change, bulk) executes without HITL approval.
- Only synthetic data. Email/SMS/webhooks are simulated.
