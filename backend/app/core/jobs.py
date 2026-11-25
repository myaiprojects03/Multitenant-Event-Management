"""Enqueue background jobs onto arq (Redis). The worker lives in app/workers/main.py."""

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

_pool: ArqRedis | None = None


async def get_job_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def enqueue(function: str, *args) -> str | None:
    job = await (await get_job_pool()).enqueue_job(function, *args)
    return job.job_id if job else None


async def close_job_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None
