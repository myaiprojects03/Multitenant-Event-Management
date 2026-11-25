# Phased build plan

Source spec: [spec.txt](spec.txt) (extracted from the .docx). The deadlines in the spec and in this plan are ignored: build as fast as possible, one phase at a time, and finish each phase's "Done when" before moving on.

## Progress
- [x] Phase 0: compose stack up, `/health` OK (db + pgvector + redis).
- [x] Phase 1: 26 tables + RLS (enabled and forced, policy-per-command), 3 DB roles, 6 reporting views + anonymized prospects view, demo JWT login, `RequestContext`, seed (2 tenants, 3 events, ~760 attendees, 88 sessions, 65 exhibitors, planted patterns verified). 38 tests incl. raw-SQL isolation suite pass.
- [x] Phase 2: LLM gateway (fast/smart model chains with fallback, hard timeout, retries with backoff on 408/429/5xx/network, Pydantic structured output, token counting), local fastembed service, scoped Redis cache with event-version invalidation, per-user rate limits (minute + day), telemetry (usage rows under RLS, redaction, request ids, access log, organizer summary/recent API, LLM self-test endpoint). Live check: both tiers return validated objects in ~1 s and are logged. 65 tests + 2 live tests pass.
- [x] Phase 3: Event assistant. Ingestion of 7 entity types into text cards (documents chunked), hash-based incremental indexing (297 cards in ~10 s; unchanged re-runs embed nothing). Hybrid retrieval (pgvector + full-text, RRF) under RLS. Flash-Lite router → deterministic SQL lookups / grounded Flash answers (an answer without a valid citation becomes "I don't know") / permission-checked tools (bookmark, list bookmarks, filter exhibitors) / refusals. Sources sanitized and treated as data. Per-role cached answers. Signed, replay-protected, idempotent platform webhook → DB change → cache invalidation → arq job re-embeds only the affected cards. `POST /assistant/query`. 86 tests + 9 live acceptance checks pass. PDF upload/extraction deferred (cut-list item 5): documents are text today.
- [x] Phase 4: BI copilot. Approved-view catalog (6 views, tenant/event columns hidden). Flash plans `{sql, explanation, chart}` with few-shot examples. sqlglot guard: one statement, SELECT only, no INTO/locks/DML-in-CTE, approved views and columns only, no `*`, no schema prefixes, function allowlist (blocks set_config/current_setting/pg_sleep/dblink/file access), LIMIT ≤ 200. Runs as `app_readonly` with a 5 s timeout; views scope rows themselves. One repair attempt on guard or DB errors. Flash-Lite summary from the rows only; chart validated against result columns. Anomaly flags in code: capacity risk, drop-off robust z-score, lead-velocity z-score; LLM only explains. `POST /analytics/nl-sql`, `GET /analytics/anomalies` (organizer only). 5 demo questions + 6 unsafe requests verified live; 43 guard tests. Seed now spans anchor-2..anchor+1 so the overbooked workshop is upcoming whenever you seed.
- [x] Phase 5: Matchmaking. `profile_vectors` (985 vectors; attendee text excludes names/companies; RLS defers to `attendee_profiles`, so consent carries over) and `recommendation_events` (own rows only). Peers / sessions / speakers / exhibitors for attendees; anonymized prospects for exhibitors via a SECURITY DEFINER function. Score = similarity + shared interests + complementary roles/needs + shared bookmarks (peers), then per-user feedback (category weights from clicks/saves/dismissals, ignored-item demotion), then a diversity re-rank. Consent, connections and dismissals filtered in SQL. Template facts → one batched Flash-Lite call phrases reasons (cached by fact text). Connection flow (contact released only after accept), meeting flow (exhibitor → anonymized prospect; acceptance creates the lead), badge scan, consent switches. `GET /recommendations?type=peers|sessions|speakers|exhibitors|prospects`. 29 tests + 3 live checks.
- [ ] Phase 6: Agents + HITL

## Free stack (no card)

| Layer | Tool |
|---|---|
| LLM | Gemini API (AI Studio key): Flash-Lite = `fast` (routing), Flash = `smart` (reasoning). IDs in config. |
| Embeddings | fastembed `BAAI/bge-small-en-v1.5` (384-d), local CPU |
| Backend | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg, Alembic |
| DB | PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`) |
| Cache / queue | Redis 7 |
| Background jobs | arq (replaces n8n) |
| SQL safety | sqlglot |
| Auth | own JWT (PyJWT) for seeded demo users |
| Seed data | Faker + generator scripts |
| Frontend | Next.js + Tailwind + shadcn/ui + Recharts |
| Tests | pytest + httpx |
| Telemetry | structlog + Postgres tables + console telemetry page |
| CI | GitHub (public) + Actions |
| Local | Docker Desktop + Compose |

Optional hosting (Phase 9): Render (API, worker runs in-process), Neon/Supabase (Postgres+pgvector), Upstash (Redis), Vercel (frontend). Avoid Fly.io and Railway.

## Locked decisions
- Standalone solo portfolio project, built from scratch.
- Gemini free tier instead of OpenAI/Claude; arq workers instead of n8n.
- **Consent:** two separate opt-ins (peer networking, exhibitor matching). Exhibitors see anonymized cards; contact details are released only after the attendee accepts or scans their badge, and only then does a lead exist.
- **HITL:** the action's owner approves. Exhibitors approve their own follow-ups; organizers approve schedule changes, announcements, published summaries; bulk or flagged exhibitor actions need a second (organizer) approval.
- Email/SMS and platform webhooks are **simulated**.
- Only synthetic data (free-tier Gemini inputs may be used by Google).

## Phases

### Phase 0: Setup
Repo layout `backend/` (api, core, modules, workers, evals), `frontend/`, `infra/`, `docs/`. Compose with Postgres+pgvector and Redis, `.env.example`, FastAPI `/health` checking DB and Redis.
**Done when:** `docker compose up` starts everything and `/health` returns OK.

### Phase 1: Foundation (most important)
- Schema: tenants, events, users, user_event_roles, attendee_profiles (both consent flags), sessions, tracks, speakers, rooms, exhibitors, sponsors, faqs, documents, bookmarks, connections, meeting_requests, leads, engagement_logs (check-in/out, booth visits, clicks), embeddings, workflows, approvals, approval_policies, audit_logs, llm_usage_logs. Every row has `tenant_id` and `event_id`.
- RLS on every table; each request sets `app.tenant_id`, `app.event_id`, `app.role`, `app.user_id` in its transaction.
- Visibility: `public`, `attendee`, `exhibitor_private` (+owner), `organizer_only`.
- Auth: demo login issuing JWTs, RBAC middleware, `RequestContext` passed everywhere.
- Seed (designed backwards from demo questions): 2 tenants, 1–2 events each (healthcare summit, tech trade show), ~300–600 attendees, 40 sessions, 30 exhibitors. Planted: big drop-off at minute 20 in one session, an overbooked workshop, one exhibitor lead spike, mix of opted-in/out.
- Reporting views: session_attendance_v, lead_summary_v, engagement_v, …
**Done when:** automated test proves tenant A user gets zero rows from tenant B on every table, and an exhibitor can't read another exhibitor's leads, even with raw SQL.

### Phase 2: AI plumbing
LLM gateway (routing fast/smart, Pydantic-validated JSON, retries/backoff on 429/5xx, timeout, token counting); fastembed batch service; Redis exact-match cache keyed tenant+event+role(+user), normalized query, TTL, invalidation by event version; per-user rate limit; telemetry middleware → `llm_usage_logs` (request_id, tenant_id, event_id, user_role, route_type, latency_ms, tokens, cache hit, model, status; redacted).
**Done when:** a test Gemini call returns a validated Pydantic object and its latency/tokens are in the log table.

### Phase 3: Module 1, Event Assistant / RAG
Ingestion of entities as text cards + PDF chunks with tenant_id, event_id, entity_type, visibility_scope. Simulated webhook → arq job re-indexes one item + invalidates cache. Router (Flash-Lite): exact lookup → SQL; general → hybrid pgvector + FTS filtered by scope/visibility; action → tool; out of scope/restricted → polite fallback. Grounded answers with citations, "I don't know" when unsupported. Tools (permission-checked in code, scope from the logged-in user, never the LLM): bookmark session, filter exhibitors by category, list my bookmarks. Retrieved docs are data only. `POST /assistant/query`.
**Done when:** schedule/room questions answered correctly with citations, restricted questions refused, bookmark works.

### Phase 4: Module 3, BI Copilot / NL-to-SQL
Prompt with approved views only + examples. Flash → `{sql, explanation, chart_type}`. Guard: sqlglot parse → one statement → SELECT only → allowlisted views/columns → inject LIMIT → read-only role + `statement_timeout` → RLS scopes regardless. One repair attempt, then graceful failure. Output: summary + metrics table + chart suggestion. Anomaly flags via rules/stats (capacity risk, drop-off vs median, lead velocity z-score); LLM only explains. `POST /analytics/nl-sql`, organizer only.
**Done when:** 5 demo questions correct; DELETE, UPDATE, multi-statement, unknown tables, cross-tenant all blocked.

### Phase 5: Module 2, Matchmaking
Profile embedding + structured features (tags, seniority, looking_for/offering). Weighted score: embedding similarity, shared tags, complementary roles, session overlap. Consent enforced in SQL; exclude connected/dismissed; diversity. Targets: peers, sessions, speakers, exhibitors; exhibitors get anonymized prospects. Template reasons phrased by Flash-Lite in one batched call. Feedback: impressions, clicks, requests, saves, dismissals; demote ignored; per-user category weights. Connection/meeting flow where acceptance creates a lead. `GET /recommendations?type=peers|sessions|exhibitors`.
**Done when:** opted-out users never appear, every match has a sensible reason, dismissed items don't return.

### Phase 6: Module 4, Agents + HITL
State machine: `pending → running → awaiting_approval → approved/rejected/expired → executing → completed/failed`. Lead Qualification Agent (score, tier, reasons; drafts follow-up). Operations Agent (post-session summary, organizer approval). approval_policies (who approves what, bulk threshold → organizer). Approval API: queue, view, edit, approve/reject with reason, expiry by arq cron. Execution: simulated sender, idempotency key at approval, exponential backoff, dead-letter after N failures. `POST /workflows/trigger`, `GET /workflows/{id}`, `POST /workflows/{id}/decision`.
**Done when:** nothing sends without approval, double approval sends once, simulated failure retries then dead-letters, bulk escalates to organizer.

### Phase 7: Demo console
Role switcher (tenant, event, demo user). Tabs: Assistant (citation chips), Matches (reasons, connect/dismiss), BI Copilot (summary, table, chart, anomalies), Approvals (queue, editor), Telemetry (latency, tokens, cache hit rate, recent requests). "Try to break it" panel with preset attacks.
**Done when:** Sara (attendee), MedScan (exhibitor), Ali (organizer) story clickable end to end.

### Phase 8: Evaluation + hardening
Golden YAML sets: ~40 RAG, ~30 RBAC/isolation incl. prompt injection, ~25 NL-to-SQL + ~15 malicious, labelled match pairs, workflow failure scenarios. Targets: 0 leaks; 100% unsafe SQL blocked; ≥85% RAG correctness with valid citations; ≥80% NL-to-SQL accuracy; precision@5; p50/p95 cached vs uncached; avg tokens. Runner writes a markdown report (sample during dev, full once). Tune. CI runs unit + isolation tests (no LLM) on every push.
**Done when:** report meets targets or honestly documents shortfalls.

### Phase 9: Packaging + optional deploy
README (problem, architecture, one-command run, design decisions, eval results, limitations, production notes), architecture diagram, OpenAPI docs, demo script, 3–5 min video. Optional live deploy.
**Done when:** a stranger can clone, `docker compose up`, seed, and follow the demo script.

## Cut order if time is tight
1. Online deployment 2. Operations Agent 3. Matchmaking feedback loop (keep dismiss) 4. Anomaly flags (keep capacity risk) 5. PDF ingestion.
**Never cut:** RLS/isolation tests, SQL guardrails, HITL gates, eval report.

## Risks
Gemini quotas → caching, deterministic code, batching, sampled evals. Model IDs change → config. Free-tier data terms → synthetic data only. Scope creep → strict "Done when".
