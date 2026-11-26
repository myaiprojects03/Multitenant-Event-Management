"""Agent workflows under failure (golden/workflows.yaml).

The real engine and simulated provider, in one rolled-back transaction per scenario, with
no LLM (agents fall back to their template drafts) and the simulated provider on Redis db 15.
"""

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext, apply_context
from app.core.db import get_engine
from app.modules.workflows.engine import WorkflowEngine, WorkflowError
from app.modules.workflows.sender import SimulatedSender
from evals.harness import Harness, load, pct
from evals.results import CaseResult, SuiteResult, sample

SESSIONS = {
    # the most overbooked upcoming session (the planted FHIR workshop on fresh seed data)
    "overbooked": "SELECT s.id FROM sessions s JOIN rooms r ON r.id = s.room_id "
    "WHERE s.event_id = :e AND s.starts_at > now() "
    "AND (SELECT count(*) FROM bookmarks b WHERE b.session_id = s.id) > r.capacity "
    "ORDER BY (SELECT count(*) FROM bookmarks b WHERE b.session_id = s.id)::float / r.capacity "
    "DESC LIMIT 1",
    "ended": "SELECT s.id FROM sessions s WHERE s.event_id = :e AND s.ends_at < now() "
    "AND EXISTS (SELECT 1 FROM engagement_logs l WHERE l.session_id = s.id) "
    "ORDER BY s.starts_at LIMIT 1",
}


class _Txn:
    def __init__(self, conn):
        self.conn = conn

    async def as_(self, ctx: RequestContext) -> AsyncSession:
        await apply_context(self.conn, ctx)
        return AsyncSession(bind=self.conn, join_transaction_mode="create_savepoint")

    async def sys_rows(self, ctx: RequestContext, sql: str, params: dict) -> list[dict]:
        await apply_context(self.conn, RequestContext.system(ctx.tenant_id, ctx.event_id))
        return [dict(r) for r in (await self.conn.execute(text(sql), params)).mappings().all()]


async def _scenario(h: Harness, sc: dict, redis: Redis) -> CaseResult:
    owner = await h.login(sc["as"], sc.get("event"))
    ctx = owner.ctx
    sys_ctx = RequestContext.system(ctx.tenant_id, ctx.event_id)
    engine = WorkflowEngine(None, SimulatedSender(redis))
    refused: list[int] = []
    params = dict(sc.get("params", {}))
    async with get_engine().connect() as conn:
        trans = await conn.begin()
        t = _Txn(conn)
        try:
            if sc.get("session"):
                sid = (await t.sys_rows(ctx, SESSIONS[sc["session"]], {"e": ctx.event_id}))[0]["id"]
                params["session_id"] = str(sid)
            wf_id = await engine.trigger(await t.as_(ctx), ctx, sc["kind"], params)
            await engine.run(await t.as_(ctx), ctx, wf_id)

            for step in sc["steps"]:
                verb, *rest = step.split()
                if verb == "deliver":
                    for _ in range(12):
                        report = await engine.deliver(await t.as_(sys_ctx), sys_ctx, wf_id)
                        if report.next_in_s is None:
                            break
                    continue
                if verb == "expire":
                    await t.sys_rows(
                        ctx,
                        "UPDATE approvals SET expires_at = now() - interval "
                        "'1 minute' WHERE workflow_id = :w RETURNING id",
                        {"w": wf_id},
                    )
                    continue
                event = (sc.get("event") or "HealthTech") if rest[0] == "ali" else None
                actor = (await h.login(rest[0], event)).ctx
                try:
                    if verb == "retry_as":
                        await engine.retry_dead_letters(await t.as_(actor), actor, wf_id)
                    elif verb == "reject_as":
                        await engine.decide(
                            await t.as_(actor), actor, wf_id, "reject", "eval", None
                        )
                    elif verb == "approve_as":
                        edits = None
                        if "with_discount" in rest:
                            draft = (
                                await t.sys_rows(
                                    ctx,
                                    "SELECT draft FROM approvals WHERE "
                                    "workflow_id = :w AND status = 'pending'",
                                    {"w": wf_id},
                                )
                            )[0]["draft"]
                            m = draft["messages"][0]
                            edits = {
                                "messages": [
                                    {
                                        "lead_id": m["lead_id"],
                                        "body": m["body"] + " We can offer 20% off.",
                                    }
                                ]
                            }
                        await engine.decide(
                            await t.as_(actor), actor, wf_id, "approve", None, edits
                        )
                except WorkflowError as exc:
                    refused.append(exc.status_code)

            wf = (
                await t.sys_rows(ctx, "SELECT status FROM workflows WHERE id = :w", {"w": wf_id})
            )[0]
            acts = await t.sys_rows(
                ctx,
                "SELECT status, attempts FROM outbound_actions WHERE workflow_id = :w",
                {"w": wf_id},
            )
            chain = [
                r["required_role"]
                for r in await t.sys_rows(
                    ctx,
                    "SELECT required_role FROM approvals WHERE workflow_id = :w "
                    "ORDER BY escalation_level",
                    {"w": wf_id},
                )
            ]
        finally:
            await trans.rollback()

    got = {
        "status": wf["status"],
        "sent": sum(a["status"] == "sent" for a in acts),
        "dead_letter": sum(a["status"] == "dead_letter" for a in acts),
        "cancelled": sum(a["status"] == "cancelled" for a in acts),
        "max_attempts": max((a["attempts"] for a in acts), default=0),
        "chain": chain,
        "refused": refused,
    }
    exp = sc["expect"]
    problems = []
    for key in ("status", "sent", "dead_letter", "cancelled", "max_attempts", "chain"):
        if key in exp and exp[key] != got[key]:
            problems.append(f"{key} {got[key]} != {exp[key]}")
    for key, field in (("min_sent", "sent"), ("min_cancelled", "cancelled")):
        if key in exp and got[field] < exp[key]:
            problems.append(f"{field} {got[field]} < {exp[key]}")
    if "refused" in exp and exp["refused"] not in refused:
        problems.append(f"expected a refusal {exp['refused']}, got {refused or 'none'}")
    summary = (
        f"{got['status']}, sent {got['sent']}, dead {got['dead_letter']}, "
        f"cancelled {got['cancelled']}, chain {'>'.join(chain)}"
    )
    return CaseResult(sc["id"], not problems, "; ".join(problems) or summary, extra=got)


async def run(h: Harness, n: int | None = None, pace_s: float = 0.0) -> SuiteResult:
    settings = get_settings()
    old_backoff = settings.delivery_backoff_base_s
    settings.delivery_backoff_base_s = 0.0  # retries without waiting
    redis = Redis.from_url(settings.redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True)
    suite = SuiteResult("workflows")
    try:
        for sc in sample(load("workflows.yaml")["scenarios"], n):
            await redis.flushdb()
            try:
                suite.cases.append(await _scenario(h, sc, redis))
            except Exception as exc:
                suite.cases.append(
                    CaseResult(sc["id"], False, f"{type(exc).__name__}: {exc}", error=True)
                )
    finally:
        settings.delivery_backoff_base_s = old_backoff
        await redis.flushdb()
        await redis.aclose()
    suite.metrics = {"scenarios_passed": pct(suite.passed, len(suite.cases))}
    return suite
