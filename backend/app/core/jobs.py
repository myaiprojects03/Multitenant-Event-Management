"""Enqueue background jobs onto arq (Redis). The worker lives in app/workers/main.py.

Serverless (no worker process): the job runs right here instead, after its delay, inside the
request that queued it. Callers already enqueue after their transaction commits (webhooks after
the change is applied, workflows from BackgroundTasks), so the job sees the committed rows.
"""

import asyncio

import structlog
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

_pool: ArqRedis | None = None


async def get_job_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


log = structlog.get_logger("jobs")


async def _run_inline(function: str, args: tuple, defer_s: float | None) -> None:
    from app.workers.main import WorkerSettings

    job = {f.__name__: f for f in WorkerSettings.functions}[function]
    if defer_s:
        await asyncio.sleep(defer_s)
    try:
        await job({}, *args)
    except Exception:  # a failed job must not fail the request that queued it (as with arq)
        log.exception("inline_job_failed", function=function)


async def enqueue(function: str, *args, defer_s: float | None = None) -> str | None:
    if get_settings().serverless:
        await _run_inline(function, args, defer_s)
        return None
    job = await (await get_job_pool()).enqueue_job(
        function, *args, _defer_by=defer_s if defer_s else None
    )
    return job.job_id if job else None


async def close_job_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None
