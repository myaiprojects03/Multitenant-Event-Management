"""Phase 6: agents + HITL. 'Done when': nothing sends without approval, double approval sends
once, a simulated failure retries then dead-letters, bulk escalates to the organizer.

DB tests run in one rolled-back transaction each; the simulated provider uses Redis db 15.
No LLM here (template drafts); the live drafting check is in test_workflows_live.py.
"""

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext, apply_context
from app.core.db import dispose_engine
from app.core.redis import close_redis
from app.modules.workflows import agents as ag
from app.modules.workflows.engine import Conflict, NotAllowed, NotFound, WorkflowEngine
from app.modules.workflows.policies import Policy, approval_chain, flag_reasons
from app.modules.workflows.sender import SimulatedSender, read_inbox
from app.modules.workflows.states import InvalidTransition, check_transition
from tests.conftest import requires_db

# --- pure ---------------------------------------------------------------------------------------


def test_state_machine():
    for a, b in [
        ("pending", "running"),
        ("running", "awaiting_approval"),
        ("awaiting_approval", "approved"),
        ("approved", "executing"),
        ("executing", "completed"),
        ("executing", "failed"),
        ("failed", "executing"),
    ]:
        check_transition(a, b)
    for a, b in [
        ("pending", "executing"),  # can't skip the approval gate
        ("awaiting_approval", "executing"),
        ("running", "approved"),
        ("rejected", "approved"),
        ("completed", "executing"),
        ("expired", "approved"),
    ]:
        with pytest.raises(InvalidTransition):
            check_transition(a, b)


def test_approval_chain_escalates_bulk_and_flagged():
    p = Policy("exhibitor_follow_up_email", "exhibitor", 20, "organizer", 48)
    assert approval_chain(p, 5, False) == ["exhibitor"]
    assert approval_chain(p, 20, False) == ["exhibitor"]
    assert approval_chain(p, 21, False) == ["exhibitor", "organizer"]
    assert approval_chain(p, 1, True) == ["exhibitor", "organizer"]
    org = Policy("schedule_change", "organizer", None, None, 12)
    assert approval_chain(org, 500, True) == ["organizer"]


def test_flag_reasons():
    ok = {"medscan.example.com"}
    assert flag_reasons("Shall we book a 20-minute call next week?", ok) == []
    assert flag_reasons("See https://medscan.example.com/demo", ok) == []
    assert flag_reasons("We can offer 20% off", ok)
    assert flag_reasons("A complimentary pilot for you", ok)
    assert flag_reasons("Book at https://evil.example.net/x", ok)[0].startswith("link")
    assert flag_reasons("write to deals@other.io", ok)


def test_lead_scoring_is_deterministic():
    crit = {
        "interests": ["FHIR", "imaging"],
        "target_industries": ["Hospital"],
        "min_seniority": "manager",
    }
    hot = {
        "interests": ["FHIR", "imaging", "AI"],
        "industry": "Hospital",
        "seniority": "director",
        "met": True,
        "looking_for": ["vendors"],
    }
    cold = {
        "interests": ["billing"],
        "industry": "Retail",
        "seniority": "junior",
        "source": "badge_scan",
    }
    s_hot, why_hot = ag.score_lead(hot, crit)
    s_cold, why_cold = ag.score_lead(cold, crit)
    assert (s_hot, why_hot) == ag.score_lead(hot, crit)
    assert ag.tier_for(s_hot) == "A" and ag.tier_for(s_cold) == "D"
    assert "accepted a meeting with you" in why_hot and why_cold == [
        "visited your booth (badge scanned)"
    ]


def test_edits_cannot_change_recipients():
    draft = {
        "allowed_domains": ["medscan.example.com"],
        "messages": [
            {"lead_id": "a", "to": "x@h.example", "subject": "Hi", "body": "Call?", "flags": []},
            {"lead_id": "b", "to": "y@h.example", "subject": "Hi", "body": "Call?", "flags": []},
        ],
    }
    with pytest.raises(ag.EditRejected):
        ag.apply_edits(
            "exhibitor_follow_up_email",
            draft,
            {"messages": [{"lead_id": "a", "to": "attacker@evil.example"}]},
        )
    with pytest.raises(ag.EditRejected):
        ag.apply_edits("exhibitor_follow_up_email", draft, {"messages": [{"lead_id": "zzz"}]})
    out = ag.apply_edits(
        "exhibitor_follow_up_email",
        draft,
        {
            "messages": [
                {"lead_id": "a", "body": "Here is 15% off"},
                {"lead_id": "b", "remove": True},
            ]
        },
    )
    assert [m["lead_id"] for m in out["messages"]] == ["a"]
    assert out["messages"][0]["flags"] and out["messages"][0]["to"] == "x@h.example"
    with pytest.raises(ag.EditRejected):
        ag.apply_edits(
            "schedule_change", {"change": {}, "announcement": {}}, {"change": {"to_room_id": "x"}}
        )


# --- DB fixtures --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


@pytest.fixture
async def redis15():
    r = Redis.from_url(get_settings().redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def txn(rw_engine):
    async with rw_engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest.fixture
def as_(txn):
    async def _open(ctx: RequestContext) -> AsyncSession:
        await apply_context(txn, ctx)
        return AsyncSession(bind=txn, join_transaction_mode="create_savepoint")

    return _open


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(get_settings(), "delivery_backoff_base_s", 0.0)


@pytest.fixture
def engine(redis15):
    return WorkflowEngine(None, SimulatedSender(redis15))


async def sys_rows(txn, ctx, sql, params=None):
    await apply_context(txn, RequestContext.system(ctx.tenant_id, ctx.event_id))
    return [dict(r) for r in (await txn.execute(text(sql), params or {})).mappings().all()]


async def start(engine, as_, ctx, kind, params):
    """trigger + run the agent as the owner (what the API + worker do)."""
    db = await as_(ctx)
    wf_id = await engine.trigger(db, ctx, kind, params)
    status = await engine.run(await as_(ctx), ctx, wf_id)
    return wf_id, status


async def deliver_all(engine, as_, ctx, wf_id, rounds=10):
    sys_ctx = RequestContext.system(ctx.tenant_id, ctx.event_id)
    report = None
    for _ in range(rounds):
        report = await engine.deliver(await as_(sys_ctx), sys_ctx, wf_id)
        if report.next_in_s is None:
            break
    return report


async def actions(txn, ctx, wf_id):
    return await sys_rows(
        txn,
        ctx,
        "SELECT * FROM outbound_actions WHERE workflow_id = :w ORDER BY stage",
        {"w": wf_id},
    )


# --- triggering + agents ------------------------------------------------------------------------


@requires_db
async def test_only_the_right_roles_trigger(engine, as_, ctx_for):
    sara, maya, ali = (
        await ctx_for("sara"),
        await ctx_for("maya"),
        await ctx_for("ali", "HealthTech"),
    )
    with pytest.raises(NotAllowed):
        await engine.trigger(await as_(sara), sara, "lead_follow_up", {})
    with pytest.raises(NotAllowed):
        await engine.trigger(await as_(maya), maya, "schedule_change", {"session_id": "x"})
    with pytest.raises(NotAllowed):
        await engine.trigger(await as_(ali), ali, "lead_follow_up", {})


@requires_db
async def test_nothing_sends_without_approval(engine, as_, ctx_for, txn, fast_retries):
    maya = await ctx_for("maya")
    wf_id, status = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 5})
    assert status == "awaiting_approval"
    appr = await sys_rows(txn, maya, "SELECT * FROM approvals WHERE workflow_id = :w", {"w": wf_id})
    assert [(a["required_role"], a["status"]) for a in appr] == [("exhibitor", "pending")]
    assert appr[0]["approver_exhibitor_id"] == maya.exhibitor_id
    assert len(appr[0]["draft"]["messages"]) == 5
    # scores written back on MedScan's own leads only
    scored = await sys_rows(
        txn,
        maya,
        "SELECT count(*) AS n FROM leads WHERE exhibitor_id = :x AND score IS NULL",
        {"x": maya.exhibitor_id},
    )
    assert scored[0]["n"] == 0
    # no outbox rows, and delivery does nothing while awaiting approval
    assert await actions(txn, maya, wf_id) == []
    report = await deliver_all(engine, as_, maya, wf_id)
    assert report.status == "awaiting_approval" and report.sent == 0


@requires_db
async def test_db_refuses_outbox_rows_without_approval(engine, as_, ctx_for, txn):
    """Even code running as `system` cannot queue an action for an unapproved workflow."""
    maya = await ctx_for("maya")
    wf_id, _ = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 2})
    appr = (
        await sys_rows(txn, maya, "SELECT id FROM approvals WHERE workflow_id = :w", {"w": wf_id})
    )[0]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with txn.begin_nested():
            await txn.execute(
                text(
                    "INSERT INTO outbound_actions (tenant_id, event_id, workflow_id, approval_id, "
                    "idempotency_key, channel, recipient) VALUES (app_tenant(), app_event(), :w, "
                    ":a, 'sneaky', 'email', 'x@example.com')"
                ),
                {"w": wf_id, "a": appr["id"]},
            )


@requires_db
async def test_approve_sends_once_to_real_leads(engine, as_, ctx_for, txn, redis15):
    maya, tom, ali = await ctx_for("maya"), await ctx_for("tom"), await ctx_for("ali", "HealthTech")
    wf_id, _ = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 4})
    # competitor can't see it; organizer can see it but can't take the owner's decision
    with pytest.raises(NotFound):
        await engine.decide(await as_(tom), tom, wf_id, "approve")
    with pytest.raises(NotAllowed):
        await engine.decide(await as_(ali), ali, wf_id, "approve")

    assert await engine.decide(await as_(maya), maya, wf_id, "approve") == "executing"
    with pytest.raises(Conflict):  # double approval
        await engine.decide(await as_(maya), maya, wf_id, "approve")
    acts = await actions(txn, maya, wf_id)
    assert len(acts) == 4 and {a["status"] for a in acts} == {"queued"}
    lead_emails = {
        r["email"]
        for r in await sys_rows(
            txn,
            maya,
            "SELECT u.email FROM leads l JOIN users u ON u.id = l.attendee_user_id "
            "WHERE l.exhibitor_id = :x",
            {"x": maya.exhibitor_id},
        )
    }
    assert {a["recipient"] for a in acts} <= lead_emails

    # re-materializing (e.g. a retried request) adds nothing: keys are unique
    wf = (await sys_rows(txn, maya, "SELECT * FROM workflows WHERE id = :w", {"w": wf_id}))[0]
    appr = (
        await sys_rows(txn, maya, "SELECT * FROM approvals WHERE workflow_id = :w", {"w": wf_id})
    )[0]
    await engine._materialize(
        await as_(RequestContext.system(maya.tenant_id, maya.event_id)),
        wf,
        appr,
        appr["idempotency_key"],
        appr["draft"],
    )
    assert len(await actions(txn, maya, wf_id)) == 4

    report = await deliver_all(engine, as_, maya, wf_id)
    assert (report.status, report.sent) == ("completed", 4)
    again = await deliver_all(engine, as_, maya, wf_id)  # a duplicate delivery job
    assert again.status == "completed" and again.sent == 0
    inbox = await read_inbox(redis15, maya.event_id)
    assert len(inbox) == 4 and len({m["to"] for m in inbox}) == 4
    # the same leads are not emailed again by the next run
    _, status = await start(
        engine, as_, maya, "lead_follow_up", {"lead_ids": [a["payload"]["lead_id"] for a in acts]}
    )
    assert status == "completed"


@requires_db
async def test_bulk_escalates_to_organizer(engine, as_, ctx_for, txn):
    maya, ali = await ctx_for("maya"), await ctx_for("ali", "HealthTech")
    wf_id, _ = await start(
        engine, as_, maya, "lead_follow_up", {"max_messages": 25, "tiers": ["A", "B", "C"]}
    )
    wf = (await sys_rows(txn, maya, "SELECT output FROM workflows WHERE id = :w", {"w": wf_id}))[0]
    assert wf["output"]["n_recipients"] > 20
    assert wf["output"]["approval_chain"] == ["exhibitor", "organizer"]
    assert await engine.decide(await as_(maya), maya, wf_id, "approve") == "awaiting_approval"
    assert await actions(txn, maya, wf_id) == []  # owner approval alone is not enough
    with pytest.raises(NotAllowed):  # the exhibitor can't take the organizer step
        await engine.decide(await as_(maya), maya, wf_id, "approve")
    with pytest.raises(ag.EditRejected):  # the reviewer approves or rejects; owner edits
        await engine.decide(await as_(ali), ali, wf_id, "approve", edits={"messages": []})
    assert await engine.decide(await as_(ali), ali, wf_id, "approve", "ok") == "executing"
    levels = await sys_rows(
        txn,
        maya,
        "SELECT escalation_level, required_role, status FROM "
        "approvals WHERE workflow_id = :w ORDER BY 1",
        {"w": wf_id},
    )
    assert [(r["required_role"], r["status"]) for r in levels] == [
        ("exhibitor", "approved"),
        ("organizer", "approved"),
    ]
    assert len(await actions(txn, maya, wf_id)) == wf["output"]["n_recipients"]


@requires_db
async def test_risky_edit_escalates(engine, as_, ctx_for, txn):
    maya = await ctx_for("maya")
    wf_id, _ = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 2})
    appr = (
        await sys_rows(
            txn, maya, "SELECT draft FROM approvals WHERE workflow_id = :w", {"w": wf_id}
        )
    )[0]
    lead = appr["draft"]["messages"][0]["lead_id"]
    status = await engine.decide(
        await as_(maya),
        maya,
        wf_id,
        "approve",
        edits={"messages": [{"lead_id": lead, "body": "Sign this week for 30% off!"}]},
    )
    assert status == "awaiting_approval"  # commercial terms -> organizer must look
    hist = (await sys_rows(txn, maya, "SELECT history FROM workflows WHERE id = :w", {"w": wf_id}))[
        0
    ]["history"]
    assert "escalated to organizer (flagged: commercial terms" in hist[-1]["note"]


@requires_db
async def test_reject_needs_reason_and_sends_nothing(engine, as_, ctx_for, txn):
    maya = await ctx_for("maya")
    wf_id, _ = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 2})
    with pytest.raises(Exception, match="reason"):
        await engine.decide(await as_(maya), maya, wf_id, "reject")
    assert await engine.decide(await as_(maya), maya, wf_id, "reject", "tone is off") == "rejected"
    assert await actions(txn, maya, wf_id) == []
    with pytest.raises(Conflict):
        await engine.decide(await as_(maya), maya, wf_id, "approve")


@requires_db
async def test_approval_expires(engine, as_, ctx_for, txn):
    maya = await ctx_for("maya")
    wf_id, _ = await start(engine, as_, maya, "lead_follow_up", {"max_messages": 2})
    await sys_rows(
        txn,
        maya,
        "UPDATE approvals SET expires_at = now() - interval '1 minute' "
        "WHERE workflow_id = :w RETURNING id",
        {"w": wf_id},
    )
    assert await engine.decide(await as_(maya), maya, wf_id, "approve") == "expired"
    assert await actions(txn, maya, wf_id) == []


# --- delivery failures --------------------------------------------------------------------------


@requires_db
async def test_transient_failure_retries_then_succeeds(engine, as_, ctx_for, txn, fast_retries):
    maya = await ctx_for("maya")
    wf_id, _ = await start(
        engine, as_, maya, "lead_follow_up", {"max_messages": 2, "simulate": {"fail_times": 2}}
    )
    await engine.decide(await as_(maya), maya, wf_id, "approve")
    sys_ctx = RequestContext.system(maya.tenant_id, maya.event_id)
    first = await engine.deliver(await as_(sys_ctx), sys_ctx, wf_id)
    assert (first.status, first.retrying, first.next_in_s is not None) == ("executing", 2, True)
    report = await deliver_all(engine, as_, maya, wf_id)
    assert (report.status, report.sent) == ("completed", 2)
    assert {a["attempts"] for a in await actions(txn, maya, wf_id)} == {3}


@requires_db
async def test_backoff_is_exponential(engine, as_, ctx_for, txn, monkeypatch):
    monkeypatch.setattr(get_settings(), "delivery_backoff_base_s", 10.0)
    maya = await ctx_for("maya")
    wf_id, _ = await start(
        engine, as_, maya, "lead_follow_up", {"max_messages": 1, "simulate": {"fail_times": 5}}
    )
    await engine.decide(await as_(maya), maya, wf_id, "approve")
    sys_ctx = RequestContext.system(maya.tenant_id, maya.event_id)
    r1 = await engine.deliver(await as_(sys_ctx), sys_ctx, wf_id)
    assert 9 < r1.next_in_s <= 10
    r_early = await engine.deliver(await as_(sys_ctx), sys_ctx, wf_id)  # not due yet: no attempt
    assert (await actions(txn, maya, wf_id))[0]["attempts"] == 1 and r_early.sent == 0
    await sys_rows(
        txn,
        maya,
        "UPDATE outbound_actions SET next_attempt_at = now() WHERE workflow_id = :w RETURNING id",
        {"w": wf_id},
    )
    r2 = await engine.deliver(await as_(sys_ctx), sys_ctx, wf_id)
    assert 19 < r2.next_in_s <= 20


@requires_db
async def test_persistent_failure_dead_letters(engine, as_, ctx_for, txn, fast_retries):
    maya = await ctx_for("maya")
    wf_id, _ = await start(
        engine, as_, maya, "lead_follow_up", {"max_messages": 2, "simulate": {"fail_times": 10}}
    )
    await engine.decide(await as_(maya), maya, wf_id, "approve")
    report = await deliver_all(engine, as_, maya, wf_id)
    assert (report.status, report.dead_letter, report.sent) == ("failed", 2, 0)
    acts = await actions(txn, maya, wf_id)
    assert {(a["status"], a["attempts"]) for a in acts} == {
        ("dead_letter", get_settings().delivery_max_attempts)
    }
    assert "503" in acts[0]["last_error"]
    logged = await sys_rows(
        txn,
        maya,
        "SELECT count(*) AS n FROM audit_logs WHERE action = "
        "'workflow.action.dead_letter' AND target_id = :w",
        {"w": str(wf_id)},
    )
    assert logged[0]["n"] == 2


# --- operations agent ---------------------------------------------------------------------------


async def fhir_workshop(txn, ctx):
    return (
        await sys_rows(
            txn,
            ctx,
            "SELECT s.id, s.room_id, (SELECT count(*) FROM bookmarks b WHERE "
            "b.session_id = s.id) AS n FROM sessions s WHERE s.title LIKE 'Hands-on Workshop: "
            "Building FHIR%'",
        )
    )[0]


@requires_db
async def test_schedule_change_moves_room_then_announces(engine, as_, ctx_for, txn, fast_retries):
    ali, sara = await ctx_for("ali", "HealthTech"), await ctx_for("sara")
    ws = await fhir_workshop(txn, ali)
    wf_id, status = await start(engine, as_, ali, "schedule_change", {"session_id": ws["id"]})
    assert status == "awaiting_approval"
    draft = (
        await sys_rows(txn, ali, "SELECT draft FROM approvals WHERE workflow_id = :w", {"w": wf_id})
    )[0]["draft"]
    change = draft["change"]
    assert change["to_capacity"] >= ws["n"] > change["from_capacity"]
    # the room has not moved yet
    db_s = await as_(sara)
    assert (
        await db_s.execute(text("SELECT room_id FROM sessions WHERE id = :s"), {"s": ws["id"]})
    ).scalar() == ws["room_id"]
    await engine.decide(
        await as_(ali),
        ali,
        wf_id,
        "approve",
        edits={"announcement": {"subject": "Bigger room for the FHIR workshop"}},
    )
    acts = await actions(txn, ali, wf_id)
    assert [a["channel"] for a in acts][:1] == ["platform_api"]
    assert sum(a["channel"] == "email" for a in acts) == ws["n"]
    assert {a["subject"] for a in acts if a["channel"] == "email"} == {
        "Bigger room for the FHIR workshop"
    }
    report = await deliver_all(engine, as_, ali, wf_id)
    assert (report.status, report.sent) == ("completed", ws["n"] + 1)
    assert ("session", str(ws["id"])) in report.reindex
    db_s = await as_(sara)
    assert (
        str(
            (
                await db_s.execute(
                    text("SELECT room_id FROM sessions WHERE id = :s"), {"s": ws["id"]}
                )
            ).scalar()
        )
        == change["to_room_id"]
    )


@requires_db
async def test_failed_platform_call_cancels_announcements_and_can_be_retried(
    engine, as_, ctx_for, txn, fast_retries
):
    ali = await ctx_for("ali", "HealthTech")
    ws = await fhir_workshop(txn, ali)
    wf_id, _ = await start(
        engine,
        as_,
        ali,
        "schedule_change",
        {"session_id": ws["id"], "simulate": {"permanent": True}},
    )
    await engine.decide(await as_(ali), ali, wf_id, "approve")
    report = await deliver_all(engine, as_, ali, wf_id)
    assert (report.status, report.dead_letter, report.cancelled) == ("failed", 1, ws["n"])
    acts = await actions(txn, ali, wf_id)
    assert acts[0]["attempts"] == 1  # permanent errors are not retried
    # nobody was told about a room change that did not happen
    assert all(a["status"] == "cancelled" for a in acts if a["channel"] == "email")

    maya = await ctx_for("maya")
    with pytest.raises(NotAllowed):
        await engine.retry_dead_letters(await as_(maya), maya, wf_id)
    assert await engine.retry_dead_letters(await as_(ali), ali, wf_id) == ws["n"] + 1
    report = await deliver_all(engine, as_, ali, wf_id)
    assert (report.status, report.sent) == ("completed", ws["n"] + 1)


@requires_db
async def test_session_summary_publishes_for_attendees(engine, as_, ctx_for, txn):
    ali, sara = await ctx_for("ali", "HealthTech"), await ctx_for("sara")
    past = (
        await sys_rows(
            txn,
            ali,
            "SELECT id FROM sessions WHERE ends_at < now() - interval "
            "'1 day' ORDER BY starts_at LIMIT 1",
        )
    )[0]
    wf_id, status = await start(engine, as_, ali, "session_summary", {"session_id": past["id"]})
    assert status == "awaiting_approval"
    db_s = await as_(sara)
    before = (await db_s.execute(text("SELECT count(*) FROM documents"))).scalar()
    await engine.decide(await as_(ali), ali, wf_id, "approve")
    report = await deliver_all(engine, as_, ali, wf_id)
    assert report.status == "completed" and report.reindex[0][0] == "document"
    db_s = await as_(sara)
    after = (await db_s.execute(text("SELECT count(*) FROM documents"))).scalar()
    assert after == before + 1  # visible to attendees, after approval only

    future = (await sys_rows(txn, ali, "SELECT id FROM sessions WHERE starts_at > now() LIMIT 1"))[
        0
    ]
    _, status = await start(engine, as_, ali, "session_summary", {"session_id": future["id"]})
    assert status == "failed"


@requires_db
async def test_due_work_function_is_context_free_only(txn, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    await apply_context(txn, ali)
    rows = (await txn.execute(text("SELECT count(*) FROM workflow_due_work()"))).scalar()
    assert rows == 0  # a request context sees nothing from the sweeper function


@requires_db
async def test_unknown_session_fails_cleanly(engine, as_, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    _, status = await start(engine, as_, ali, "schedule_change", {"session_id": str(uuid.uuid4())})
    assert status == "failed"


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
async def api(txn, monkeypatch):
    """HTTP client on the rolled-back transaction; job enqueues are recorded, not sent."""
    from typing import Annotated

    import httpx
    from fastapi import Depends

    from app.api import workflows as wf_api
    from app.core import security
    from app.main import app

    enqueued: list[tuple] = []

    async def _enqueue(function, *args, **kw):
        enqueued.append((function, *args))

    monkeypatch.setattr(wf_api.jobs, "enqueue", _enqueue)

    async def _db(ctx: Annotated[RequestContext, Depends(security.get_ctx)]):
        await apply_context(txn, ctx)
        async with AsyncSession(bind=txn, join_transaction_mode="create_savepoint") as s:
            yield s
            await s.commit()

    app.dependency_overrides[security.get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.enqueued = enqueued
        yield c
    app.dependency_overrides.clear()


async def login(api, email, event_id=None):
    body = {"email": email} | ({"event_id": str(event_id)} if event_id else {})
    r = await api.post("/auth/demo-login", json=body)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@requires_db
async def test_workflow_api_flow(api, engine, as_, ctx_for):
    from tests.conftest import PERSONA_EMAILS as E

    maya, ali = await ctx_for("maya"), await ctx_for("ali", "HealthTech")
    hm, hs, ht = [await login(api, E[p]) for p in ("maya", "sara", "tom")]
    ha = await login(api, E["ali"], ali.event_id)

    body = {"kind": "lead_follow_up", "params": {"max_messages": 2}}
    assert (await api.post("/workflows/trigger", json=body, headers=hs)).status_code == 403
    bad = {"kind": "lead_follow_up", "params": {"max_messages": 999}}
    assert (await api.post("/workflows/trigger", json=bad, headers=hm)).status_code == 422
    r = await api.post("/workflows/trigger", json=body, headers=hm)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    wf_id = r.json()["workflow_id"]
    assert api.enqueued[-1] == ("run_workflow_job", str(maya.tenant_id), str(maya.event_id), wf_id)

    await engine.run(await as_(maya), maya, uuid.UUID(wf_id))  # what the worker does
    d = (await api.get(f"/workflows/{wf_id}", headers=hm)).json()
    assert d["status"] == "awaiting_approval" and d["actions"] == []
    assert len(d["approvals"][0]["draft"]["messages"]) == 2
    assert (await api.get(f"/workflows/{wf_id}", headers=ht)).status_code == 404  # competitor
    assert len((await api.get("/approvals", headers=hm)).json()) == 1
    assert (await api.get("/approvals", headers=ha)).json() == []  # not the organizer's step

    r = await api.post(f"/workflows/{wf_id}/decision", json={"decision": "approve"}, headers=ha)
    assert r.status_code == 403
    r = await api.post(f"/workflows/{wf_id}/decision", json={"decision": "approve"}, headers=hm)
    assert r.status_code == 200 and r.json()["status"] == "executing"
    assert r.json()["action_counts"] == {"queued": 2}
    assert api.enqueued[-1][0] == "deliver_workflow_job"
    r = await api.post(f"/workflows/{wf_id}/decision", json={"decision": "approve"}, headers=hm)
    assert r.status_code == 409
    assert (await api.get("/workflows/outbox/sent", headers=hm)).status_code == 403
    listed = (await api.get("/workflows?status=executing", headers=hm)).json()
    assert [str(w["id"]) for w in listed] == [wf_id]
