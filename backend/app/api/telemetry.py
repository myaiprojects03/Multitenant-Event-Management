"""Organizer telemetry: latency, tokens, cache hit rate, recent LLM calls; plus an LLM self-test.

All reads go through RLS (llm_usage_logs is staff-only), so an organizer only ever sees their
own tenant + event.
"""

import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.cache import ResponseCache
from app.core.llm import LLMError, get_llm
from app.core.ratelimit import rate_limited
from app.core.redis import get_redis
from app.core.security import DbDep, OrganizerDep
from app.core.telemetry import UsageRecord, record_usage

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


class RouteStats(BaseModel):
    route_type: str
    calls: int
    errors: int
    cache_hits: int
    p50_ms: float | None
    p95_ms: float | None
    avg_tokens: float | None


class Summary(BaseModel):
    window_minutes: int
    calls: int
    errors: int
    cache_hit_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    p50_uncached_ms: float | None
    p50_cached_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    avg_tokens_per_call: float | None
    cost_usd: float
    by_route: list[RouteStats]
    by_model: dict[str, int]


_STATS = """
  count(*) AS calls,
  count(*) FILTER (WHERE status <> 'ok') AS errors,
  count(*) FILTER (WHERE cache_hit) AS cache_hits,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
"""


@router.get("/summary", response_model=Summary)
async def summary(
    _: OrganizerDep, db: DbDep, minutes: Annotated[int, Query(ge=1, le=60 * 24 * 30)] = 1440
) -> Summary:
    window = {"m": minutes}
    where = "WHERE created_at > now() - make_interval(mins => :m)"
    totals = (
        (
            await db.execute(
                text(
                    f"""SELECT {_STATS},
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                    FILTER (WHERE NOT cache_hit) AS p50_uncached_ms,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                    FILTER (WHERE cache_hit) AS p50_cached_ms,
                  coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                  coalesce(sum(completion_tokens), 0) AS completion_tokens,
                  avg(prompt_tokens + completion_tokens) FILTER (WHERE NOT cache_hit)
                    AS avg_tokens_per_call,
                  coalesce(sum(cost_usd), 0) AS cost_usd
                FROM llm_usage_logs {where}"""
                ),
                window,
            )
        )
        .mappings()
        .one()
    )
    routes = (
        await db.execute(
            text(
                f"""SELECT route_type, {_STATS},
                  avg(prompt_tokens + completion_tokens) FILTER (WHERE NOT cache_hit)
                    AS avg_tokens
                FROM llm_usage_logs {where} GROUP BY route_type ORDER BY calls DESC"""
            ),
            window,
        )
    ).mappings()
    models = (
        await db.execute(
            text(
                f"""SELECT model, count(*) AS n FROM llm_usage_logs {where}
                AND model IS NOT NULL GROUP BY model ORDER BY n DESC"""
            ),
            window,
        )
    ).all()
    calls = totals["calls"]
    return Summary(
        window_minutes=minutes,
        cache_hit_rate=round(totals["cache_hits"] / calls, 3) if calls else None,
        by_route=[RouteStats(**r) for r in routes],
        by_model={m: n for m, n in models},
        **{k: v for k, v in totals.items() if k != "cache_hits"},
    )


class UsageRow(BaseModel):
    created_at: str
    request_id: str
    user_role: str | None
    route_type: str
    model: str | None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit: bool
    status: str
    error: str | None


@router.get("/recent", response_model=list[UsageRow])
async def recent(
    _: OrganizerDep, db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[UsageRow]:
    rows = (
        await db.execute(
            text(
                """SELECT created_at::text, request_id, user_role, route_type, model, latency_ms,
                      prompt_tokens, completion_tokens, cache_hit, status, error
                   FROM llm_usage_logs ORDER BY created_at DESC LIMIT :n"""
            ),
            {"n": limit},
        )
    ).mappings()
    return [UsageRow(**r) for r in rows]


class SelfTestRequest(BaseModel):
    question: str = Field(
        default="Which room is the FHIR workshop in?", min_length=3, max_length=500
    )
    tier: Literal["fast", "smart"] = "fast"
    use_cache: bool = True


class Intent(BaseModel):
    """Toy router output: proves structured output end to end."""

    intent: Literal["exact_lookup", "general_question", "action", "out_of_scope"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)


class SelfTestResponse(BaseModel):
    result: Intent
    model: str | None
    cache_hit: bool
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


@router.post(
    "/llm-selftest",
    response_model=SelfTestResponse,
    dependencies=[Depends(rate_limited("llm"))],
)
async def llm_selftest(
    body: SelfTestRequest,
    ctx: OrganizerDep,
    redis: Annotated[Redis, Depends(get_redis)],
) -> SelfTestResponse:
    cache = ResponseCache(redis, default_ttl_s=300)
    namespace = f"selftest:{body.tier}"
    t0 = time.perf_counter()
    if body.use_cache and (hit := await cache.get(ctx, namespace, body.question)):
        latency = round((time.perf_counter() - t0) * 1000)
        await record_usage(ctx, UsageRecord("telemetry.selftest", latency, cache_hit=True))
        return SelfTestResponse(
            result=Intent(**hit),
            model=None,
            cache_hit=True,
            latency_ms=latency,
            prompt_tokens=0,
            completion_tokens=0,
        )
    try:
        resp = await get_llm().generate(
            f"Classify this event-assistant question.\nQuestion: {body.question}",
            ctx=ctx,
            route_type="telemetry.selftest",
            tier=body.tier,
            schema=Intent,
            system="You route questions for an event assistant. Answer only with the schema.",
            temperature=0,
            thinking_level="low" if body.tier == "smart" else None,
        )
    except LLMError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "LLM unavailable") from exc
    assert resp.parsed is not None
    if body.use_cache:
        await cache.set(ctx, namespace, body.question, resp.parsed.model_dump())
    return SelfTestResponse(
        result=resp.parsed,
        model=resp.model,
        cache_hit=False,
        latency_ms=resp.latency_ms,
        prompt_tokens=resp.prompt_tokens,
        completion_tokens=resp.completion_tokens,
    )
