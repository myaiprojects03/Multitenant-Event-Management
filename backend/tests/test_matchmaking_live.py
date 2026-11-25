"""Phase 5 live check (RUN_LIVE_LLM=1): LLM-phrased reasons stay grounded in the facts."""

import os

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import apply_context
from app.core.db import dispose_engine
from app.core.llm import LLMGateway
from app.modules.matchmaking.service import MatchmakingService
from tests.conftest import requires_db

pytestmark = [
    requires_db,
    pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1"),
]

STOP = {
    "you",
    "your",
    "they",
    "their",
    "both",
    "and",
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "which",
    "with",
    "for",
    "at",
    "is",
    "are",
    "to",
    "this",
    "that",
    "share",
    "interest",
}


@pytest.fixture
async def svc():
    r = Redis.from_url(get_settings().redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True)
    await r.flushdb()
    yield MatchmakingService(LLMGateway(), r)
    await r.flushdb()
    await r.aclose()
    await dispose_engine()


@pytest.mark.parametrize(
    "persona, kind", [("sara", "peers"), ("sara", "exhibitors"), ("maya", "prospects")]
)
async def test_phrased_reasons_are_grounded(svc, ctx_for, rw_engine, persona, kind):
    ctx = await ctx_for(persona)
    async with rw_engine.connect() as conn:  # roll back: impressions must not leak into tests
        trans = await conn.begin()
        await apply_context(conn, ctx)
        db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        r = await svc.recommend(db, ctx, kind, limit=5, phrase=True)
        await db.close()
        await trans.rollback()
    assert r.phrased and r.items
    for item in r.items:
        print(f"\n{item.title}: {item.reason}\n   facts: {item.facts[:2]}")
        assert 0 < len(item.reason) <= 240
        fact_words = {w.strip(",.()%").lower() for f in item.facts[:2] for w in f.split()} - STOP
        reason_words = {w.strip(",.()%").lower() for w in item.reason.split()} - STOP
        assert fact_words & reason_words, "reason must reuse the facts' content"
