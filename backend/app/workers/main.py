"""arq worker entry point: `arq app.workers.main.WorkerSettings`.

Jobs (re-indexing, lead qualification, sending, approval expiry) are added in later phases.
"""

from arq.connections import RedisSettings

from app.core.config import get_settings


async def ping(ctx: dict) -> str:
    return "pong"


class WorkerSettings:
    functions = [ping]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
