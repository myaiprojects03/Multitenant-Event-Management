"""Hosted-demo housekeeping (DEMO_AUTO_SEED=1): keep the synthetic data anchored to today.

The seed is dated relative to its run day ("yesterday's lead spike", "tomorrow's overbooked
workshop"), so a hosted demo needs a fresh seed each day. There is no shell or cron on the free
tier, so the API checks on startup and the in-process worker re-checks daily. A Redis lock
keeps two checks from seeding at once.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.redis import get_redis

log = structlog.get_logger("demo")
LOCK = "demo:seeding"
SUMMIT = "healthtech-summit-2026"


async def is_stale() -> bool:
    """True when there is no demo data or it was generated for an earlier day."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            starts_on = (
                await conn.execute(
                    text("SELECT starts_on FROM events WHERE slug = :s"), {"s": SUMMIT}
                )
            ).scalar()
    finally:
        await engine.dispose()
    expected = datetime.now(UTC).date() - timedelta(days=2)  # the summit runs anchor-2 .. anchor+1
    return starts_on != expected


async def _forget_yesterday(redis) -> None:
    """Cached answers carry yesterday's dates and the simulated inbox yesterday's sends."""
    from app.core.cache import ResponseCache
    from app.core.context import RequestContext

    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            events = (await conn.execute(text("SELECT tenant_id, id FROM events"))).all()
    finally:
        await engine.dispose()
    cache = ResponseCache(redis)
    for tenant_id, event_id in events:
        await cache.invalidate_event(RequestContext.system(tenant_id, event_id))
    async for key in redis.scan_iter("sim:*"):
        await redis.delete(key)


async def refresh(force: bool = False) -> str:
    redis = get_redis()
    if not await redis.set(LOCK, "1", nx=True, ex=1800):
        return "already running"
    try:
        if not force and not await is_stale():
            return "fresh"
        from app.ingest import run as ingest
        from app.seed.__main__ import load
        from app.seed.generator import generate

        t0 = time.perf_counter()
        log.info("demo_reseed_started")
        rows = await asyncio.to_thread(generate, None, 42)
        await load(rows)  # one transaction: readers see the old data or the new, never half
        await ingest()  # embeddings for the assistant (the seed wipes them)
        await _forget_yesterday(redis)
        log.info("demo_reseed_done", seconds=round(time.perf_counter() - t0, 1))
        return "reseeded"
    except Exception:
        log.exception("demo_reseed_failed")
        return "failed"
    finally:
        await redis.delete(LOCK)
