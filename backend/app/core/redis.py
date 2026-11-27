from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.loops import current_loop

_redis: Redis | None = None
_redis_loop = None


def get_redis() -> Redis:
    global _redis, _redis_loop
    stale = get_settings().serverless and _redis_loop is not current_loop()
    if _redis is None or stale:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        _redis_loop = current_loop()
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None
