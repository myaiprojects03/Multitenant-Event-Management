"""arq worker entry point: `arq app.workers.main.WorkerSettings`."""

import uuid
from dataclasses import asdict

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from app.core import jobs
from app.core.cache import ResponseCache
from app.core.config import get_settings
from app.core.context import RequestContext, scoped_session
from app.core.db import dispose_engine, get_engine
from app.core.embeddings import get_embedder
from app.core.llm import get_llm
from app.core.redis import close_redis, get_redis
from app.modules.assistant.indexer import index_event, reindex_entity
from app.modules.workflows.engine import WorkflowEngine, owner_context
from app.modules.workflows.sender import SimulatedSender


async def ping(ctx: dict) -> str:
    return "pong"


async def reindex_entity_job(
    ctx: dict, tenant_id: str, event_id: str, entity_type: str, entity_id: str
) -> dict:
    """Re-embed one changed entity (plus dependent cards) and invalidate the event cache."""
    sys_ctx = RequestContext.system(uuid.UUID(tenant_id), uuid.UUID(event_id))
    async with scoped_session(sys_ctx) as db:
        stats = await reindex_entity(db, get_embedder(), entity_type, uuid.UUID(entity_id))
    await ResponseCache(get_redis()).invalidate_event(sys_ctx)
    return asdict(stats)


async def index_event_job(ctx: dict, tenant_id: str, event_id: str) -> dict:
    sys_ctx = RequestContext.system(uuid.UUID(tenant_id), uuid.UUID(event_id))
    async with scoped_session(sys_ctx) as db:
        stats = await index_event(db, get_embedder())
    await ResponseCache(get_redis()).invalidate_event(sys_ctx)
    return asdict(stats)


# --- workflows -----------------------------------------------------------------------------------


# The API enqueues right after its transaction; if the job wins that race it waits briefly.
COMMIT_WAIT_TRIES = 5


async def run_workflow_job(
    ctx: dict, tenant_id: str, event_id: str, workflow_id: str, attempt: int = 0
) -> str:
    """Run the agent step with the owner's rights (not system)."""
    sys_ctx = RequestContext.system(uuid.UUID(tenant_id), uuid.UUID(event_id))
    async with scoped_session(sys_ctx) as db:
        row = (
            (await db.execute(text("SELECT * FROM workflows WHERE id = :id"), {"id": workflow_id}))
            .mappings()
            .first()
        )
    if row is None:
        if attempt < COMMIT_WAIT_TRIES:
            await jobs.enqueue(
                "run_workflow_job", tenant_id, event_id, workflow_id, attempt + 1, defer_s=1
            )
        return "missing"  # otherwise the sweeper picks it up
    owner = owner_context(dict(row))
    async with scoped_session(owner) as db:
        return await WorkflowEngine(get_llm()).run(db, owner, uuid.UUID(workflow_id))


async def deliver_workflow_job(
    ctx: dict, tenant_id: str, event_id: str, workflow_id: str, attempt: int = 0
) -> dict:
    """One delivery pass; schedules itself again for the next retry (exponential backoff)."""
    sys_ctx = RequestContext.system(uuid.UUID(tenant_id), uuid.UUID(event_id))
    engine = WorkflowEngine(None, SimulatedSender(get_redis()))
    async with scoped_session(sys_ctx) as db:
        report = await engine.deliver(db, sys_ctx, uuid.UUID(workflow_id))
    if report.status in ("awaiting_approval", "approved") and attempt < COMMIT_WAIT_TRIES:
        await jobs.enqueue(
            "deliver_workflow_job", tenant_id, event_id, workflow_id, attempt + 1, defer_s=1
        )
    if report.reindex:  # applied changes are visible to the assistant right away
        await ResponseCache(get_redis()).invalidate_event(sys_ctx)
        for entity_type, entity_id in report.reindex:
            await jobs.enqueue("reindex_entity_job", tenant_id, event_id, entity_type, entity_id)
    if report.next_in_s is not None:
        await jobs.enqueue(
            "deliver_workflow_job",
            tenant_id,
            event_id,
            workflow_id,
            defer_s=max(report.next_in_s, 0.5),
        )
    return asdict(report)


async def sweep_workflows(ctx: dict) -> dict:
    """Safety net: start workflows whose job was lost, expire stale approvals, re-drive due
    deliveries. workflow_due_work() returns ids only and only on a context-free connection."""
    async with get_engine().connect() as conn:
        due = (await conn.execute(text("SELECT * FROM workflow_due_work()"))).mappings().all()
    counts = {"run": 0, "expire": 0, "deliver": 0}
    for d in due:
        t, e, w = str(d["tenant_id"]), str(d["event_id"]), str(d["workflow_id"])
        if d["work"] == "run":
            await jobs.enqueue("run_workflow_job", t, e, w)
        elif d["work"] == "deliver":
            await jobs.enqueue("deliver_workflow_job", t, e, w)
        else:
            sys_ctx = RequestContext.system(d["tenant_id"], d["event_id"])
            async with scoped_session(sys_ctx) as db:
                await WorkflowEngine(None).expire(db, sys_ctx, d["workflow_id"])
        counts[d["work"]] += 1
    return counts


async def refresh_demo_job(ctx: dict) -> str:
    """Daily (DEMO_AUTO_SEED): reseed when the demo data is dated before today."""
    from app import demo

    return await demo.refresh()


async def shutdown(ctx: dict) -> None:
    await dispose_engine()
    await close_redis()
    await jobs.close_job_pool()


class WorkerSettings:
    functions = [
        ping,
        reindex_entity_job,
        index_event_job,
        run_workflow_job,
        deliver_workflow_job,
        refresh_demo_job,
    ]
    cron_jobs = [
        cron(sweep_workflows, second={0, 30}, run_at_startup=True)
        if not get_settings().sweep_interval_min
        else cron(
            sweep_workflows,
            minute=set(range(0, 60, get_settings().sweep_interval_min)),
            second=0,
            run_at_startup=True,
        )
    ] + ([cron(refresh_demo_job, hour={0}, minute={5})] if get_settings().demo_auto_seed else [])
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 5  # arq retries failed jobs (Retry / exceptions) up to this many times
