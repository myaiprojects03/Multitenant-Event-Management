"""Phase 5: matchmaking. 'Done when': opted-out users never appear, every match has a
sensible reason, dismissed items don't come back. Plus the consent / lead flows.

Everything runs inside one DB transaction per test that is rolled back, including the API
tests (the app's DB dependency is overridden to join that transaction).
"""

import uuid
from collections import Counter
from typing import Annotated

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.context import RequestContext, apply_context
from app.core.db import dispose_engine
from app.core.redis import close_redis, get_redis
from app.main import app
from app.modules.matchmaking import scoring as sc
from app.modules.matchmaking.service import MatchmakingService, NotAllowed
from tests.conftest import PERSONA_EMAILS, requires_db

# --- pure scoring -------------------------------------------------------------------------------


def test_needs_met_and_complementary_roles():
    assert sc.needs_met(["mentorship", "vendors"], ["mentoring"]) == [("mentorship", "mentoring")]
    assert sc.needs_met(["investors"], ["mentoring"]) == []
    assert ("buyer", "seller") in sc.COMPLEMENTARY and ("buyer", "buyer") not in sc.COMPLEMENTARY


def test_feedback_adjustment():
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [
        {"target_type": "peer", "target_id": a, "kind": "impression", "category": "buyer", "n": 6},
        {"target_type": "peer", "target_id": b, "kind": "impression", "category": "seller", "n": 6},
        {"target_type": "peer", "target_id": b, "kind": "click", "category": "seller", "n": 2},
        {"target_type": "peer", "target_id": a, "kind": "dismiss", "category": "mentor", "n": 1},
    ]
    fb = sc.Feedback.from_rows(rows)
    assert fb.adjustment("peer", a, "buyer") == pytest.approx(-0.16)  # ignored 6x: (6-2)*0.04
    assert fb.adjustment("peer", b, "seller") == pytest.approx(0.08)  # engaged: no penalty
    assert fb.adjustment("peer", uuid.uuid4(), "mentor") == pytest.approx(-0.06)


def test_diversify_spreads_categories():
    items = [
        type("I", (), {"category": c, "score": s})()
        for c, s in [("a", 0.9), ("a", 0.89), ("a", 0.88), ("b", 0.84), ("c", 0.83), ("a", 0.87)]
    ]
    picked = [i.category for i in sc.diversify(items, 4)]
    # a(0.90); then a(0.89-0.08=0.81) loses to b(0.84); then c(0.83); then a again
    assert picked == ["a", "b", "c", "a"]
    assert [i.category for i in sc.diversify(items, 4)][:1] == ["a"]  # best item always first


# --- DB fixtures --------------------------------------------------------------------------------

pytestmark_db = requires_db


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


@pytest.fixture
async def txn(rw_engine):
    async with rw_engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest.fixture
def as_(txn):
    """`db = await as_(ctx)` -> AsyncSession on the shared rolled-back transaction."""
    sessions = []

    async def _open(ctx: RequestContext) -> AsyncSession:
        await apply_context(txn, ctx)
        s = AsyncSession(bind=txn, join_transaction_mode="create_savepoint")
        sessions.append(s)
        return s

    return _open


async def sys_query(txn, ctx, sql, params=None):
    await apply_context(txn, RequestContext.system(ctx.tenant_id, ctx.event_id))
    return (await txn.execute(text(sql), params or {})).mappings().all()


def service():
    return MatchmakingService(None, get_redis())  # no LLM: template reasons


async def recs(as_, ctx, kind, limit=25):
    return await service().recommend(await as_(ctx), ctx, kind, limit, phrase=False)


# --- consent -------------------------------------------------------------------------------------


@requires_db
async def test_peers_are_only_networking_opted_in(ctx_for, as_, txn):
    sara, omar = await ctx_for("sara"), await ctx_for("omar")
    r = await recs(as_, sara, "peers")
    assert len(r.items) == 25
    ids = [i.target_id for i in r.items]
    flags = await sys_query(
        txn,
        sara,
        "SELECT networking_opt_in, user_id FROM attendee_profiles WHERE id = ANY(:i)",
        {"i": ids},
    )
    assert all(f["networking_opt_in"] for f in flags)
    assert omar.user_id not in {f["user_id"] for f in flags}


@requires_db
async def test_opting_out_removes_a_peer_immediately(ctx_for, as_, txn):
    sara = await ctx_for("sara")
    top = (await recs(as_, sara, "peers", 5)).items[0]
    peer_user = uuid.UUID(top.data["user_id"])
    peer = RequestContext(sara.tenant_id, sara.event_id, "attendee", peer_user)
    await (await as_(peer)).execute(
        text("UPDATE attendee_profiles SET networking_opt_in = false WHERE user_id = app_user()")
    )
    after = await recs(as_, sara, "peers")
    assert top.target_id not in {i.target_id for i in after.items}


@requires_db
async def test_opted_out_attendee_gets_no_peer_recommendations(ctx_for, as_):
    r = await recs(as_, await ctx_for("omar"), "peers")
    assert r.items == [] and "Turn on networking" in r.message


@requires_db
async def test_prospects_are_anonymized_and_matching_opted_in(ctx_for, as_, txn):
    maya, omar = await ctx_for("maya"), await ctx_for("omar")
    r = await recs(as_, maya, "prospects")
    assert r.items and all(i.title.startswith("P-") for i in r.items)
    for i in r.items:
        assert not {"user_id", "display_name", "company", "email"} & set(i.data)
    flags = await sys_query(
        txn,
        maya,
        "SELECT exhibitor_matching_opt_in, user_id FROM attendee_profiles WHERE id = ANY(:i)",
        {"i": [i.target_id for i in r.items]},
    )
    assert all(f["exhibitor_matching_opt_in"] for f in flags)
    assert omar.user_id not in {f["user_id"] for f in flags}


@requires_db
async def test_recommendation_types_are_role_bound(ctx_for, as_):
    with pytest.raises(NotAllowed):
        await recs(as_, await ctx_for("sara"), "prospects")
    with pytest.raises(NotAllowed):
        await recs(as_, await ctx_for("maya"), "peers")
    with pytest.raises(NotAllowed):
        await recs(as_, await ctx_for("ali", "HealthTech"), "sessions")


# --- reasons, dismissals, feedback ----------------------------------------------------------------


@pytest.mark.parametrize(
    "persona, kind",
    [
        ("sara", "peers"),
        ("sara", "sessions"),
        ("sara", "speakers"),
        ("sara", "exhibitors"),
        ("maya", "prospects"),
        ("leo", "peers"),
        ("leo", "exhibitors"),
    ],
)
@requires_db
async def test_every_match_has_a_reason_grounded_in_facts(ctx_for, as_, persona, kind):
    r = await recs(as_, await ctx_for(persona), kind, 10)
    assert r.items
    for i in r.items:
        assert i.facts and i.reason.strip(". ")
        assert i.reason.startswith(i.facts[0])  # template reason = top facts, nothing invented


@pytest.mark.parametrize(
    "persona, kind, target",
    [
        ("sara", "peers", "peer"),
        ("sara", "sessions", "session"),
        ("sara", "speakers", "speaker"),
        ("sara", "exhibitors", "exhibitor"),
        ("maya", "prospects", "prospect"),
    ],
)
@requires_db
async def test_dismissed_items_never_come_back(ctx_for, as_, persona, kind, target):
    ctx = await ctx_for(persona)
    first = await recs(as_, ctx, kind, 5)
    gone = {i.target_id for i in first.items[:2]}
    db = await as_(ctx)
    for tid in gone:
        await db.execute(
            text(
                "INSERT INTO recommendation_events (tenant_id, event_id, user_id, target_type, "
                "target_id, kind) VALUES (app_tenant(), app_event(), app_user(), :t, :id, "
                "'dismiss')"
            ),
            {"t": target, "id": tid},
        )
    for _ in range(2):  # and not on later calls either
        again = await recs(as_, ctx, kind, 25)
        assert not gone & {i.target_id for i in again.items}


@requires_db
async def test_ignored_items_are_pushed_down(ctx_for, as_):
    sara = await ctx_for("sara")
    first = await recs(as_, sara, "speakers", 5)
    top = first.items[0]
    scores = []
    for _ in range(5):
        r = await recs(as_, sara, "speakers", 25)
        scores.append(next((i.score for i in r.items if i.target_id == top.target_id), None))
    assert scores[-1] is None or scores[-1] < top.score - 0.1


@requires_db
async def test_clicks_boost_their_category(ctx_for, as_):
    sara = await ctx_for("sara")
    before = await recs(as_, sara, "exhibitors", 25)
    low = before.items[-1]
    db = await as_(sara)
    for _ in range(5):
        await db.execute(
            text(
                "INSERT INTO recommendation_events (tenant_id, event_id, user_id, target_type, "
                "target_id, kind, category) VALUES (app_tenant(), app_event(), app_user(), "
                "'exhibitor', :id, 'click', :c)"
            ),
            {"id": uuid.uuid4(), "c": low.category},
        )
    after = await recs(as_, sara, "exhibitors", 25)
    same = [i for i in after.items if i.target_id == low.target_id]
    assert same and same[0].score > low.score


@requires_db
async def test_top_ten_is_diverse(ctx_for, as_):
    r = await recs(as_, await ctx_for("sara"), "peers", 10)
    counts = Counter(i.category for i in r.items)
    assert len(counts) >= 3 and max(counts.values()) <= 6


# --- API flows (rolled back) ---------------------------------------------------------------------


@pytest.fixture
async def api(txn):
    async def _db(ctx: Annotated[RequestContext, Depends(security.get_ctx)]):
        await apply_context(txn, ctx)
        async with AsyncSession(bind=txn, join_transaction_mode="create_savepoint") as s:
            yield s
            await s.commit()  # releases the savepoint; the outer transaction is rolled back

    app.dependency_overrides[security.get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def login(api, email):
    r = await api.post("/auth/demo-login", json={"email": email})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def email_of(txn, ctx, user_id):
    return (await sys_query(txn, ctx, "SELECT email FROM users WHERE id = :u", {"u": user_id}))[0][
        "email"
    ]


@requires_db
async def test_connection_flow_releases_contact_only_after_accept(api, ctx_for, txn):
    sara = await ctx_for("sara")
    hs = await login(api, PERSONA_EMAILS["sara"])
    peers = (await api.get("/recommendations?type=peers&limit=3&phrase=false", headers=hs)).json()
    peer_user = peers["items"][0]["data"]["user_id"]
    r = await api.post("/connections", json={"user_id": peer_user}, headers=hs)
    assert r.status_code == 201 and r.json()["status"] == "pending"
    mine = (await api.get("/connections", headers=hs)).json()
    pending = next(c for c in mine if str(c["other_user_id"]) == peer_user)
    assert pending["contact_email"] is None  # nothing shared yet

    hp = await login(api, await email_of(txn, sara, peer_user))
    assert (
        await api.post(f"/connections/{r.json()['id']}/respond", json={"accept": True}, headers=hp)
    ).status_code == 200
    mine = (await api.get("/connections", headers=hs)).json()
    accepted = next(c for c in mine if str(c["other_user_id"]) == peer_user)
    assert accepted["status"] == "accepted" and accepted["contact_email"]
    # the requester can't accept on the other person's behalf
    assert (
        await api.post(f"/connections/{r.json()['id']}/respond", json={"accept": True}, headers=hs)
    ).status_code == 404


@requires_db
async def test_cannot_connect_with_or_as_opted_out(api, ctx_for):
    hs, ho = await login(api, PERSONA_EMAILS["sara"]), await login(api, PERSONA_EMAILS["omar"])
    omar = await ctx_for("omar")
    r = await api.post("/connections", json={"user_id": str(omar.user_id)}, headers=hs)
    assert r.status_code == 404
    sara = await ctx_for("sara")
    r = await api.post("/connections", json={"user_id": str(sara.user_id)}, headers=ho)
    assert r.status_code == 403


@requires_db
async def test_prospect_meeting_creates_lead_only_on_accept(api, ctx_for, txn):
    maya = await ctx_for("maya")
    hm, ht = await login(api, PERSONA_EMAILS["maya"]), await login(api, PERSONA_EMAILS["tom"])
    prospect = (
        await api.get("/recommendations?type=prospects&limit=1&phrase=false", headers=hm)
    ).json()["items"][0]
    r = await api.post("/meetings", json={"profile_id": prospect["target_id"]}, headers=hm)
    assert r.status_code == 201
    meeting_id = r.json()["id"]
    listed = next(
        m
        for m in (await api.get("/meetings", headers=hm)).json()
        if str(m["id"]) == str(meeting_id)
    )
    assert listed["attendee_name"] == "Anonymous prospect"  # no identity before consent

    user_id = (
        await sys_query(
            txn,
            maya,
            "SELECT user_id FROM attendee_profiles WHERE id = :p",
            {"p": prospect["target_id"]},
        )
    )[0]["user_id"]
    ha = await login(api, await email_of(txn, maya, user_id))
    assert (
        await api.post(f"/meetings/{meeting_id}/respond", json={"accept": True}, headers=ht)
    ).status_code == 404  # competitor can't touch it
    r = await api.post(f"/meetings/{meeting_id}/respond", json={"accept": True}, headers=ha)
    assert r.status_code == 200 and r.json()["lead_id"]
    listed = next(
        m
        for m in (await api.get("/meetings", headers=hm)).json()
        if str(m["id"]) == str(meeting_id)
    )
    assert listed["attendee_name"] != "Anonymous prospect"  # consent given: identity released
    again = await api.post(f"/meetings/{meeting_id}/respond", json={"accept": True}, headers=ha)
    assert again.status_code == 409


@requires_db
async def test_cannot_request_meeting_with_opted_out_attendee(api, ctx_for, txn):
    maya, omar = await ctx_for("maya"), await ctx_for("omar")
    hm = await login(api, PERSONA_EMAILS["maya"])
    omar_profile = (
        await sys_query(
            txn, maya, "SELECT id FROM attendee_profiles WHERE user_id = :u", {"u": omar.user_id}
        )
    )[0]["id"]
    r = await api.post("/meetings", json={"profile_id": str(omar_profile)}, headers=hm)
    assert r.status_code == 404 and "not available" in r.text


@requires_db
async def test_badge_scan_creates_lead_in_event_only(api, ctx_for):
    hm = await login(api, PERSONA_EMAILS["maya"])
    omar, leo = await ctx_for("omar"), await ctx_for("leo")
    # in-person badge scan is consent even for someone opted out of online matching
    r = await api.post(
        "/leads/badge-scan", json={"attendee_user_id": str(omar.user_id)}, headers=hm
    )
    assert r.status_code == 201 and r.json()["lead_id"]
    r = await api.post("/leads/badge-scan", json={"attendee_user_id": str(leo.user_id)}, headers=hm)
    assert r.status_code == 400 and "not valid" in r.text
    hs = await login(api, PERSONA_EMAILS["sara"])
    assert (
        await api.post(
            "/leads/badge-scan", json={"attendee_user_id": str(omar.user_id)}, headers=hs
        )
    ).status_code == 403


@requires_db
async def test_consent_switch_and_feedback_via_api(api):
    hs = await login(api, PERSONA_EMAILS["sara"])
    xs = (await api.get("/recommendations?type=exhibitors&limit=5&phrase=false", headers=hs)).json()
    top = xs["items"][0]["target_id"]
    r = await api.post(
        "/recommendations/feedback",
        headers=hs,
        json={"target_type": "exhibitor", "target_id": top, "action": "dismiss"},
    )
    assert r.status_code == 204
    xs = (
        await api.get("/recommendations?type=exhibitors&limit=25&phrase=false", headers=hs)
    ).json()
    assert top not in {i["target_id"] for i in xs["items"]}

    sess = (await api.get("/recommendations?type=sessions&limit=1&phrase=false", headers=hs)).json()
    sid = sess["items"][0]["target_id"]
    await api.post(
        "/recommendations/feedback",
        headers=hs,
        json={"target_type": "session", "target_id": sid, "action": "save"},
    )
    sess = (
        await api.get("/recommendations?type=sessions&limit=25&phrase=false", headers=hs)
    ).json()
    assert sid not in {i["target_id"] for i in sess["items"]}  # saved = bookmarked

    r = await api.put("/me/consent", json={"networking_opt_in": False}, headers=hs)
    assert r.json() == {"networking_opt_in": False, "exhibitor_matching_opt_in": True}
    peers = (await api.get("/recommendations?type=peers&phrase=false", headers=hs)).json()
    assert peers["items"] == [] and "networking" in peers["message"]
    bad = await api.post(
        "/recommendations/feedback",
        headers=hs,
        json={"target_type": "prospect", "target_id": top, "action": "click"},
    )
    assert bad.status_code == 403
