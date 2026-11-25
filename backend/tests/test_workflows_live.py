"""Phase 6 live check (RUN_LIVE_LLM=1): agent drafts are grounded and policy-clean.
Runs in a rolled-back transaction, so lead scores and workflows don't persist."""

import os
import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.context import apply_context
from app.core.db import dispose_engine
from app.core.llm import LLMGateway
from app.modules.workflows.engine import WorkflowEngine
from tests.conftest import requires_db

pytestmark = [
    requires_db,
    pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1"),
]


async def owner_scalar(sql: str, params: dict | None = None):
    """Pick test inputs as the owner (no RLS), never used by the code under test."""
    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as c:
        value = (await c.execute(text(sql), params or {})).scalar()
    await engine.dispose()
    return value


@pytest.fixture
async def run_live(rw_engine):
    """run_live(ctx, kind, params) -> (status, draft) inside a transaction that is rolled back."""

    async def _go(ctx, kind, params):
        engine = WorkflowEngine(LLMGateway())
        async with rw_engine.connect() as conn:
            trans = await conn.begin()
            await apply_context(conn, ctx)
            db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            wf_id = await engine.trigger(db, ctx, kind, params)
            status = await engine.run(db, ctx, wf_id)
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT w.output, a.draft FROM workflows w LEFT JOIN approvals a "
                            "ON a.workflow_id = w.id WHERE w.id = :w"
                        ),
                        {"w": wf_id},
                    )
                )
                .mappings()
                .first()
            )
            await db.close()
            await trans.rollback()
        return status, dict(row)

    yield _go
    await dispose_engine()


async def test_follow_up_drafts_are_grounded(run_live, ctx_for):
    maya = await ctx_for("maya")
    status, row = await run_live(maya, "lead_follow_up", {"max_messages": 4})
    assert status == "awaiting_approval" and row["output"]["used_llm"]
    for m in row["draft"]["messages"]:
        print(f"\n--- to {m['to_name']} ({m['tier']} {m['score']}): {m['subject']}\n{m['body']}")
        first = m["to_name"].split()[0]
        body = m["body"]
        assert first in body, "addresses the lead by first name"
        assert "MedScan" in body and "[" not in body, "signed, no placeholders"
        assert 30 <= len(body.split()) <= 160
        assert not m["flags"], f"no prices/discounts/outside links: {m['flags']}"
        assert not re.search(r"\d+\s*%|\$\d", body)
        if not any("meeting" in r for r in m["reasons"]):
            # a booth visit is not a conversation: no invented discussions
            assert not re.search(
                r"learn(ed|ing) about|discuss(ed|ing) your|our (chat|talk)", body, re.IGNORECASE
            ), body


async def test_session_recap_uses_real_numbers(run_live, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    sid = await owner_scalar(
        "SELECT s.id FROM sessions s WHERE s.event_id = :e AND s.ends_at < now() "
        "AND EXISTS (SELECT 1 FROM engagement_logs l WHERE l.session_id = s.id) "
        "ORDER BY s.starts_at LIMIT 1",
        {"e": ali.event_id},
    )
    status, row = await run_live(ali, "session_summary", {"session_id": str(sid)})
    assert status == "awaiting_approval" and row["output"]["used_llm"]
    doc, facts = row["draft"]["document"], row["draft"]["facts"]
    print(f"\n{doc['title']}\n{doc['body']}\nfacts: attended={facts['attended']}")
    assert doc["title"].startswith("Recap:")
    assert str(facts["attended"]) in doc["body"], "uses the real attendance figure"
    assert doc["body"].count("\n- ") + doc["body"].startswith("- ") >= 3


async def test_schedule_change_announcement(run_live, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    sid = await owner_scalar(
        "SELECT id FROM sessions WHERE title LIKE 'Hands-on Workshop: Building FHIR%'"
    )
    status, row = await run_live(ali, "schedule_change", {"session_id": str(sid)})
    assert status == "awaiting_approval"
    c, a = row["draft"]["change"], row["draft"]["announcement"]
    print(f"\n{a['subject']}\n{a['body']}")
    assert c["to_room"] in a["body"] and c["from_room"] in a["body"]
