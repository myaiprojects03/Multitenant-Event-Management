# Multi-Tenant Event AI Platform

An AI service layer for a multi-tenant event platform (conferences, trade shows): a permission-aware event assistant (RAG), attendee matchmaking, a guarded NL-to-SQL BI copilot, and agent workflows with human-in-the-loop approvals. Tenant and event isolation is enforced by PostgreSQL row-level security.

> Work in progress. See [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Run locally

Requires Docker Desktop.

```bash
cp .env.example .env                 # then add GEMINI_API_KEY and a long random JWT_SECRET
docker compose up --build -d         # Postgres+pgvector, Redis, API (runs migrations), worker, console
docker compose exec api python -m app.seed   # load synthetic demo data + index it for the assistant
```

- Demo console: http://localhost:3000
- API docs: http://localhost:8000/docs

Console development (hot reload, proxies to the API on :8000): `cd frontend; npm install; npm run dev`.

Try it:

```bash
curl http://localhost:8000/auth/demo-users                      # seeded demo personas
curl -X POST http://localhost:8000/auth/demo-login \
     -H 'content-type: application/json' -d '{"email":"sara@northwind-health.example.com"}'
curl http://localhost:8000/me -H "Authorization: Bearer <access_token>"
```

Ask the event assistant (as Sara):

```bash
curl -X POST http://localhost:8000/assistant/query -H "Authorization: Bearer <access_token>" \
     -H 'content-type: application/json' -d '{"question":"Which room is the FHIR workshop in?"}'
```

## Demo console

A Next.js console (`frontend/`) for clicking through the whole story. Pick a demo user on the sign-in page; the "View as" menu switches user or event at any time. Sessions are per browser tab, so Maya and Ali can sit side by side in two tabs for the approval flow.

| Role | Screens |
|---|---|
| Attendee (Sara) | Event assistant with numbered sources, recommendations (people, sessions, speakers, exhibitors) with reasons and connect / save / request meeting / dismiss, the two consent switches, connections and meeting requests |
| Exhibitor (Maya) | Anonymized prospects, meeting requests and badge scans, lead follow-ups (run the agent, review and edit each email, approve or reject), approvals |
| Organizer (Ali) | BI copilot (summary, chart, SQL, rows) next to anomaly flags, approvals (including escalated exhibitor drafts), workflows (room changes for overbooked sessions, recaps, failure simulation, retry of dead letters, sent outbox), telemetry |
| Everyone | Security tests: preset attacks (prompt injection, cross-tenant questions, a competitor's leads, destructive and raw-table SQL, organizer-only endpoints, a forged JWT, an unsigned webhook) run against the live API, each checked and marked Held or Leaked |

The browser only talks to the console's own origin; `/api/*` is proxied to the backend (`API_URL`), so the API needs no CORS. The visual design is deliberately plain: one accent color, flat sections and tables, text status labels.

## Event assistant

1. **Router** (Gemini Flash-Lite, structured output) picks one route: exact lookup, general question, action, out of scope, or restricted.
2. **Exact lookups** (rooms, times, booths) are answered straight from SQL, so the answer is exact and costs no generation call.
3. **General questions** use hybrid retrieval: pgvector similarity plus Postgres full-text search, merged with reciprocal rank fusion. Retrieval runs under the caller's RLS, so content the caller may not see is never retrieved. Gemini Flash then writes a grounded answer with `[n]` citations. An answer without a valid citation is replaced with "I don't know".
4. **Actions** (bookmark a session, list bookmarks, filter exhibitors) are permission-checked in code. All ids come from the token, never from the model.
5. Retrieved text is treated as untrusted data: it is wrapped in a delimited block that it cannot escape, and the model is told never to follow instructions inside it.
6. **Freshness:** a signed platform webhook (HMAC, replay window, idempotent) applies the change, invalidates the event's cache immediately, and queues a background job that re-embeds only the affected cards.

## BI copilot (organizers)

`POST /analytics/nl-sql` with `{"question": "Which exhibitors generated the most qualified leads yesterday?"}` returns the SQL, the rows, a short summary and a chart suggestion. Every generated query passes through layered defenses:

1. **sqlglot guard (deny by default):**
   - exactly one SELECT, with no `SELECT INTO`, row locks or data changes hidden in CTEs;
   - only the approved reporting views and their listed columns, with no `*` and no schema prefixes;
   - a function allowlist, so `set_config`, `current_setting`, `pg_sleep`, `dblink` and file access are rejected;
   - a LIMIT of at most 200.
2. **Read-only role:** the query runs as `app_readonly`, which can read only those views, inside a read-only transaction with a 5 s timeout.
3. **Self-scoping views:** the views filter rows to the caller's tenant and event themselves, so a query that leaves out the filter, or names another tenant, still gets only the caller's data.
4. **One repair:** if the guard or the database rejects the SQL, the error goes back to the model once for a corrected query.

`GET /analytics/anomalies` computes flags in code (capacity risk, drop-off outliers by robust z-score, lead-velocity spikes by z-score). The LLM only writes the explanation.

## Matchmaking

`GET /recommendations?type=peers|sessions|speakers|exhibitors` (attendees) or `?type=prospects` (exhibitors) returns ranked matches, each with a "why we matched" reason.

- **Consent:** two separate opt-ins. Peers only see attendees who opted in to networking. Exhibitors only see attendees who opted in to exhibitor matching, and then as anonymized cards (`P-7B7E92`, industry, seniority, interests) produced by a SECURITY DEFINER function that never returns identity. Contact details are released only after a connection or meeting is accepted, or a badge is scanned, and only then does a lead exist.
- **Scoring:** vector similarity, shared interests, complementary roles and needs (buyer↔seller, mentorship↔mentoring) and shared bookmarks. Per-user feedback then adjusts the score: clicks and saves boost a category, dismissals hide an item permanently, and items shown repeatedly but ignored are pushed down. A diversity re-rank keeps the top 10 varied.
- **Reasons:** built from concrete facts, then phrased by one batched Flash-Lite call (cached by fact text). Without the LLM, the template reason is used.
- **Flows:** `POST /connections` with `/respond`, `POST /meetings` with `/respond`, `POST /leads/badge-scan`, `PUT /me/consent`, and `POST /recommendations/feedback`.

## Agent workflows with human approval

`POST /workflows/trigger` starts an agent; the response returns at once (`pending`) and the worker runs it.

- **Lead Qualification Agent** (exhibitors, `lead_follow_up`):
  - scores the exhibitor's own leads in code against its private criteria (interests, target industry, seniority, accepted meetings), so the score is reproducible, and writes score and tier back;
  - Gemini Flash drafts one follow-up email per top lead from those facts only, with no prices, discounts or outside links;
  - leads already contacted are skipped.
- **Operations Agent** (organizers):
  - `session_summary` drafts a recap of an ended session from its description and real attendance numbers;
  - `schedule_change` picks a free room that fits an overbooked session's demand (in code) and drafts the attendee announcement.

Every draft stops at an approval gate (`awaiting_approval`):

- **Who approves:** the owner approves (exhibitors their follow-ups, organizers schedule changes and published recaps), via `GET /approvals` and `POST /workflows/{id}/decision` with `approve`/`reject`, a reason, and optional edits.
- **What edits can change:** text only. Recipients are re-read from the database at execution, never taken from the draft.
- **Escalation:** more than 20 recipients, or a draft (including an edited one) that mentions prices, discounts or outside links, adds an organizer approval. Unanswered approvals expire.
- **Nothing executes without approval:** external effects exist only as rows in an outbox table, and the database policy on that table rejects any row whose workflow is not fully approved, even from system code.
- **Double approval sends once:** the decision locks the workflow, a decided approval can't be decided again, and every action carries a unique idempotency key derived from the final approval. The simulated email provider dedupes on that key too.
- **Failures:** transient errors retry with exponential backoff. After 4 attempts, or on a permanent error, the action is dead-lettered (and audited), and later stages are cancelled, so no announcement goes out for a room change that failed. An organizer can re-drive it with `POST /workflows/{id}/retry`.
- **Simulated effects:** email goes to a fake provider (`GET /workflows/outbox/sent`), room changes are applied to the schedule, and recaps are published as attendee documents. Changed content is re-indexed, so the assistant answers with the new room.

## Evaluation

`backend/evals` runs golden test sets against the live API and writes [backend/evals/reports/latest.md](backend/evals/reports/latest.md). Latest full run:

| Metric | Target | Result |
|---|---|---|
| Data leaks (isolation, RBAC, consent, prompt injection, SQL) | 0 | 0 |
| Unsafe SQL blocked | 100% | 100% (30 raw statements, 12 adversarial prompts) |
| RAG answer correctness | >= 85% | 100% (42/42) |
| Citations valid (exist, same event, visible to the role) | 100% | 100% |
| NL-to-SQL execution accuracy | >= 80% | 100% (25/25) |
| Matchmaking precision@5 | above base rate | 79% vs 54% random (1.45x) |
| Workflow failure scenarios | 100% | 13/13 |
| Latency p50 / p95, uncached vs cached | report | 4.0 s / 10.3 s vs 38 ms / 82 ms |

What is in the sets:

- **RAG (42):** logistics FAQs and documents, booths, schedule lookups, topical questions, permitted private content (an exhibitor's own pricing, the organizer's green-room code), and questions it must decline. Generated facts (titles, rooms, times, speakers) are looked up from the database at run time, so the sets survive a reseed. Every citation is checked: it must exist, belong to the persona's event and be visible to the role.
- **Isolation and RBAC (36):** planted secrets (green-room code, operations extension, pricing sheets) asked by roles that must not see them, five prompt-injection styles, a competitor's leads, other tenants and events, organizer-only endpoints, a forged JWT, and consent in recommendations.
- **NL-to-SQL (25):** each question has a reference query; results are compared by value through the same guard and read-only role.
- **SQL safety (47):** 30 raw attacks (DDL/DML, stacked statements, base tables, catalogs, `set_config`, file and network functions, locks), each tried against the guard and the database separately (the read-only role alone stops 23 of 30), 5 legitimate queries that must still pass, and 12 adversarial prompts.
- **Matchmaking:** precision@5 against declared-interest labels for 17 attendees and exhibitors, compared with the precision of 5 random eligible candidates, plus hand-labelled must-haves.
- **Workflows (13):** sends only after approval, double approval, wrong approver, reject, bulk and risky-edit escalation, transient and persistent failures, a failed room move cancelling its announcements and then retried, expiry, a recap published once.

```powershell
cd backend; .\.venv\Scripts\python -m evals.run            # full run (~20 min, Gemini quota)
cd backend; .\.venv\Scripts\python -m evals.run --sample 5 # quick pass
cd backend; .\.venv\Scripts\python -m evals.run --no-llm   # deterministic part (CI runs it, --strict)
```

Caveats, also in the report: during the latest run the smart Gemini models were out of free-tier quota, so every answer came from the Flash-Lite fallbacks; cases hit by Gemini outages were re-run once and merged; the one miss the first run found (an exhibitor's own-discount question answered with its booth) was fixed and the affected suites re-run in full. Correctness is checked with value and string matching against database facts, not an LLM judge.

## How isolation works

- Three database roles: the **owner** (migrations and seeding only), **app_rw** (what the API uses; not the owner, so every RLS policy applies), and **app_readonly** (NL-to-SQL; can only `SELECT` from approved reporting views).
- Each request verifies a JWT, builds a `RequestContext` (tenant, event, role, user, exhibitor) and pins it to the DB transaction with `set_config(..., is_local => true)`.
- Every table has RLS enabled. Policies read the context, so a query with no context returns zero rows. RLS binds every role except a table's owner, so the request roles are never owners, superusers or `BYPASSRLS` (tested), and the API refuses to start in production, and re-indexing refuses to run, on a connection that would skip RLS.
- Visibility levels: `public`, `attendee`, `exhibitor_private` (owner exhibitor only), `organizer_only`.
- Reporting views filter on the same context and are organizer-only. Exhibitors get a separate anonymized prospects view: no names, companies or contact details, and only attendees who opted in to exhibitor matching.
- Tests in [backend/tests/test_isolation.py](backend/tests/test_isolation.py) run raw SQL as `app_rw` for several personas and assert zero cross-tenant, cross-event and cross-exhibitor rows on every table.

## Deployment (free tier)

The API runs as a single Render web service (Docker), the database on Neon (Postgres + pgvector) and the console on Vercel, all on free plans.

- **API:** one Render web service built from [backend/Dockerfile](backend/Dockerfile). The container runs migrations on boot, its own Redis (queue, cache and rate limits only; losing it on a restart is fine), the job worker in-process, and has the embedding model baked in. It sleeps after 15 minutes idle; the first request then takes about a minute.
- **Demo data:** the seed is dated relative to the day it runs, so [.github/workflows/reseed-demo.yml](.github/workflows/reseed-demo.yml) migrates, reseeds and re-indexes the Neon database nightly, and on demand. It runs on GitHub Actions because embedding about 1,300 cards on Render's 0.1 CPU takes far too long.
- **Secrets:** `GEMINI_API_KEY`, `DATABASE_OWNER_URL` (Neon's owner connection string; the direct endpoint is used even if the pooled one is pasted) and `APP_SECRET`, from which the app role passwords and the JWT and webhook secrets are derived. The role passwords are re-applied on every boot.
- **Console:** Vercel project with root directory `frontend` and `API_URL` set to the Render URL. Browser calls go to `/api/*` on the console, which proxies them to the API (no CORS).

Steps:
1. Neon: create a project (Postgres 16, AWS US East) and copy its connection string. Generate `APP_SECRET` with `python -c "import secrets; print(secrets.token_hex(32))"`.
2. GitHub repo secrets `DATABASE_OWNER_URL` and `APP_SECRET`, then run **Actions > Reseed demo** (it also creates the schema).
3. Render: **New > Web Service**, connect this repo, language Docker, root directory `backend`, instance type Free, health check path `/health`. Environment: `APP_ENV=production`, `RUN_WORKER_IN_PROCESS=true`, `SWEEP_INTERVAL_MIN=30`, plus the three secrets above.
4. Vercel: import the repo, set the root directory to `frontend` and `API_URL=https://<service>.onrender.com`, then deploy.

## Demo personas

| Persona | Tenant / event | Role |
|---|---|---|
| Sara Ahmed | Northwind / HealthTech Summit | attendee, opted in to both |
| Omar Haddad | Northwind / HealthTech Summit | attendee, opted out of both |
| Maya Chen (MedScan) | Northwind / HealthTech Summit | exhibitor |
| Tom Becker (CardioVue, MedScan's competitor) | Northwind / HealthTech Summit | exhibitor |
| Ali Raza | Northwind / both events | organizer |
| Leo Park, Nina Patel (CloudForge), Grace Lin | Apex / DevWorld Expo | attendee, exhibitor, organizer |

Login is passwordless on purpose: this is a demo with synthetic users only.

## Layout

```
backend/    FastAPI app (api, core, modules, workers, seed), migrations, tests, evals
frontend/   Next.js demo console
infra/      SQL init scripts
docs/       spec and plan
```
