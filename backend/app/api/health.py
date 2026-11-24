import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.redis import get_redis

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_S = 3.0


async def _check_db(session: AsyncSession) -> dict:
    await session.execute(text("SELECT 1"))
    has_vector = (
        await session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    ).scalar() is not None
    if not has_vector:
        return {"status": "error", "detail": "pgvector extension not installed"}
    return {"status": "ok", "pgvector": True}


async def _check_redis(redis: Redis) -> dict:
    await redis.ping()
    return {"status": "ok"}


async def _run(check) -> dict:
    try:
        return await asyncio.wait_for(check, CHECK_TIMEOUT_S)
    except Exception as exc:  # report, never raise, from a health check
        return {"status": "error", "detail": type(exc).__name__}


@router.get("/health")
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> JSONResponse:
    db, cache = await asyncio.gather(_run(_check_db(session)), _run(_check_redis(redis)))
    ok = db["status"] == "ok" and cache["status"] == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": {"db": db, "redis": cache}},
    )
