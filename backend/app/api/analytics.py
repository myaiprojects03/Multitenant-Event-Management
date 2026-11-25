"""Organizer-only analytics: NL-to-SQL copilot and anomaly flags."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis

from app.core.llm import get_llm
from app.core.ratelimit import rate_limited
from app.core.redis import get_redis
from app.core.security import DbDep, OrganizerDep
from app.modules.analytics.anomalies import anomaly_report
from app.modules.analytics.nl_sql import NlSqlService
from app.modules.analytics.schemas import AnomalyReport, NlSqlRequest, NlSqlResponse

router = APIRouter(prefix="/analytics", tags=["analytics"])
RedisDep = Annotated[Redis, Depends(get_redis)]


@router.post("/nl-sql", response_model=NlSqlResponse, dependencies=[Depends(rate_limited("llm"))])
async def nl_sql(
    body: NlSqlRequest, ctx: OrganizerDep, db: DbDep, redis: RedisDep
) -> NlSqlResponse:
    """Ask an analytics question in plain English. Organizer only; read-only; event-scoped."""
    return await NlSqlService(get_llm(), redis).answer(db, ctx, body.question)


@router.get("/anomalies", response_model=AnomalyReport, dependencies=[Depends(rate_limited("llm"))])
async def anomalies(
    ctx: OrganizerDep, redis: RedisDep, explain: Annotated[bool, Query()] = True
) -> AnomalyReport:
    """Capacity risk, drop-off outliers and lead-velocity spikes for the current event."""
    return await anomaly_report(ctx, get_llm(), redis, explain=explain)
