"""Per-user rate limits in Redis: a per-minute window and a per-day quota.

Fixed windows (INCR + EXPIRE in one pipeline) are enough here: the goal is protecting the
free-tier Gemini quota and stopping one user from hogging it, not exact smoothing.
"""

import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.redis import get_redis
from app.core.security import get_ctx


@dataclass
class RateLimitResult:
    allowed: bool
    minute_count: int
    minute_limit: int
    day_count: int
    day_limit: int
    retry_after_s: int


def limits_for(ctx: RequestContext) -> tuple[int, int]:
    s = get_settings()
    per_minute = (
        s.rate_limit_per_minute_organizer if ctx.role == "organizer" else s.rate_limit_per_minute
    )
    return per_minute, s.rate_limit_per_day


async def check_rate_limit(
    redis: Redis, ctx: RequestContext, bucket: str = "llm", now: float | None = None
) -> RateLimitResult:
    now = time.time() if now is None else now
    minute = int(now // 60)
    day = time.strftime("%Y%m%d", time.gmtime(now))
    who = f"{ctx.tenant_id}:{ctx.user_id}"
    m_key, d_key = f"rl:{bucket}:{who}:m:{minute}", f"rl:{bucket}:{who}:d:{day}"
    pipe = redis.pipeline(transaction=True)
    pipe.incr(m_key)
    pipe.expire(m_key, 70)
    pipe.incr(d_key)
    pipe.expire(d_key, 90_000)
    m_count, _, d_count, _ = await pipe.execute()
    per_minute, per_day = limits_for(ctx)
    if d_count > per_day:
        retry = 86_400 - int(now % 86_400)
    elif m_count > per_minute:
        retry = 60 - int(now % 60)
    else:
        retry = 0
    return RateLimitResult(retry == 0, m_count, per_minute, d_count, per_day, retry)


def rate_limited(bucket: str = "llm"):
    """FastAPI dependency: 429 with Retry-After once the user is over a limit."""

    async def _dep(
        ctx: Annotated[RequestContext, Depends(get_ctx)],
        redis: Annotated[Redis, Depends(get_redis)],
    ) -> RateLimitResult:
        result = await check_rate_limit(redis, ctx, bucket)
        if not result.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "rate limit exceeded",
                headers={"Retry-After": str(result.retry_after_s)},
            )
        return result

    return _dep
