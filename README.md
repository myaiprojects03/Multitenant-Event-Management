# Multi-Tenant Event AI Platform

An AI service layer for a multi-tenant event platform (conferences, trade shows): a permission-aware event assistant (RAG), attendee matchmaking, a guarded NL-to-SQL BI copilot, and agent workflows with human-in-the-loop approvals. Tenant and event isolation is enforced by PostgreSQL row-level security.

> Work in progress. See [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Run locally

Requires Docker Desktop.

```bash
cp .env.example .env                 # then add GEMINI_API_KEY and a long random JWT_SECRET
docker compose up --build -d         # Postgres+pgvector, Redis, API (runs migrations), worker
docker compose exec api python -m app.seed   # load synthetic demo data + index it for the assistant
curl http://localhost:8000/health
```

API docs: http://localhost:8000/docs

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

## How isolation works

- Three database roles: the **owner** (migrations and seeding only), **app_rw** (what the API uses; not the owner, so every RLS policy applies), and **app_readonly** (NL-to-SQL; can only `SELECT` from approved reporting views).
- Each request verifies a JWT, builds a `RequestContext` (tenant, event, role, user, exhibitor) and pins it to the DB transaction with `set_config(..., is_local => true)`.
- Every table has RLS enabled and forced. Policies read the context, so a query with no context returns zero rows.
- Visibility levels: `public`, `attendee`, `exhibitor_private` (owner exhibitor only), `organizer_only`.
- Reporting views filter on the same context and are organizer-only. Exhibitors get a separate anonymized prospects view: no names, companies or contact details, and only attendees who opted in to exhibitor matching.
- Tests in [backend/tests/test_isolation.py](backend/tests/test_isolation.py) run raw SQL as `app_rw` for several personas and assert zero cross-tenant, cross-event and cross-exhibitor rows on every table.

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
