"""Phase 2 'done when': a real Gemini call returns a validated Pydantic object and its latency
and tokens land in llm_usage_logs. Uses free-tier quota, so it only runs when asked:

    $env:RUN_LIVE_LLM = "1"; .\\.venv\\Scripts\\pytest tests/test_llm_live.py -s
"""

import os
import uuid
from typing import Literal

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.context import apply_context
from app.core.db import dispose_engine
from app.core.llm import LLMGateway
from tests.conftest import requires_db

pytestmark = [
    requires_db,
    pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1"),
]


class Route(BaseModel):
    intent: Literal["exact_lookup", "general_question", "action", "out_of_scope"]
    confidence: float = Field(ge=0, le=1)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    yield
    await dispose_engine()


@pytest.mark.parametrize("tier", ["fast", "smart"])
async def test_live_structured_call_is_logged(rw_engine, ctx_for, tier):
    ali = await ctx_for("ali", "HealthTech")
    route = f"live.{tier}.{uuid.uuid4().hex[:6]}"
    r = await LLMGateway().generate(
        "Question for an event assistant: 'Which room is the FHIR workshop in?'",
        ctx=ali,
        route_type=route,
        tier=tier,
        schema=Route,
        temperature=0,
        thinking_level="low" if tier == "smart" else None,
    )
    print(f"\n{tier}: {r.parsed} via {r.model} in {r.latency_ms}ms, "
          f"{r.prompt_tokens}+{r.completion_tokens} tokens, {r.attempts} attempt(s)")  # fmt: skip
    assert isinstance(r.parsed, Route) and r.parsed.intent == "exact_lookup"

    async with rw_engine.connect() as conn, conn.begin():
        await apply_context(conn, ali)
        row = (
            await conn.execute(
                text(
                    "SELECT model, latency_ms, prompt_tokens, completion_tokens, status "
                    "FROM llm_usage_logs WHERE route_type = :r"
                ),  # fmt: skip
                {"r": route},
            )
        ).one()
    assert row.status == "ok" and row.model == r.model
    assert row.latency_ms == r.latency_ms and row.prompt_tokens > 0 and row.completion_tokens > 0
