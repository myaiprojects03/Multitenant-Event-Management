"""arq worker entry point: `arq app.workers.main.WorkerSettings`."""

import uuid
from dataclasses import asdict

from arq.connections import RedisSettings

from app.core.cache import ResponseCache
from app.core.config import get_settings
from app.core.context import RequestContext, scoped_session
from app.core.db import dispose_engine
from app.core.embeddings import get_embedder
from app.core.redis import close_redis, get_redis
from app.modules.assistant.indexer import index_event, reindex_entity


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


async def shutdown(ctx: dict) -> None:
    await dispose_engine()
    await close_redis()


class WorkerSettings:
    functions = [ping, reindex_entity_job, index_event_job]
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 5  # arq retries failed jobs (Retry / exceptions) up to this many times
