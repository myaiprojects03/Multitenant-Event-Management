"""Signed platform webhooks -> DB change -> cache invalidation -> targeted re-index."""

import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.api import webhooks as webhooks_api
from app.core.cache import ResponseCache
from app.core.config import get_settings
from app.core.context import RequestContext, apply_context
from app.core.db import dispose_engine
from app.core.jobs import close_job_pool
from app.core.redis import close_redis, get_redis
from app.main import app
from app.workers.main import reindex_entity_job
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()
    await close_job_pool()


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(function, *args):
        calls.append((function, *args))
        return "job-test"

    monkeypatch.setattr(webhooks_api.jobs, "enqueue", fake_enqueue)
    return calls


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def signed(payload: dict, ts: int | None = None, secret: str | None = None):
    body = json.dumps(payload, default=str).encode()
    ts_s = str(ts or int(time.time()))
    secret = secret or get_settings().webhook_secret.get_secret_value()
    return body, {
        "content-type": "application/json",
        "x-platform-timestamp": ts_s,
        "x-platform-signature": webhooks_api.sign(secret, ts_s, body),
    }


async def _q(rw_engine, ctx, sql, params=None):
    async with rw_engine.connect() as conn, conn.begin():
        await apply_context(conn, ctx)
        return (await conn.execute(text(sql), params or {})).first()


async def fhir_session(rw_engine, ali):
    return await _q(
        rw_engine, ali,
        "SELECT s.id, r.name AS room FROM sessions s JOIN rooms r ON r.id = s.room_id "
        "WHERE s.title LIKE '%FHIR Integrations%'",
    )  # fmt: skip


def payload(ali, entity_id, room, kind="session.updated"):
    return {
        "id": f"dlv-{uuid.uuid4().hex[:10]}",
        "type": kind,
        "tenant_id": str(ali.tenant_id),
        "event_id": str(ali.event_id),
        "entity_id": str(entity_id),
        "data": {"room": room},
    }


async def test_rejects_bad_signature_and_stale_timestamp(client, ctx_for, enqueued):
    ali = await ctx_for("ali", "HealthTech")
    body, headers = signed(payload(ali, uuid.uuid4(), "Hall A"), secret="wrong-secret")
    assert (
        await client.post("/webhooks/platform", content=body, headers=headers)
    ).status_code == 401
    body, headers = signed(payload(ali, uuid.uuid4(), "Hall A"), ts=int(time.time()) - 3600)
    r = await client.post("/webhooks/platform", content=body, headers=headers)
    assert r.status_code == 401 and "stale" in r.text
    r = await client.post(
        "/webhooks/platform", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 401
    assert not enqueued


async def test_cannot_touch_another_tenants_entity(client, ctx_for, rw_engine, enqueued):
    ali, grace = await ctx_for("ali", "HealthTech"), await ctx_for("grace")
    expo_session = (await _q(rw_engine, grace, "SELECT id FROM sessions LIMIT 1")).id
    body, headers = signed(payload(ali, expo_session, "Hall A"))  # signed for ali's event
    r = await client.post("/webhooks/platform", content=body, headers=headers)
    assert r.status_code == 404 and not enqueued


async def test_room_change_flows_through_to_assistant_data(client, ctx_for, rw_engine, enqueued):
    ali = await ctx_for("ali", "HealthTech")
    sys_ctx = RequestContext.system(ali.tenant_id, ali.event_id)
    original = await fhir_session(rw_engine, ali)
    cache = ResponseCache(get_redis())
    version_before = await cache.event_version(ali)
    try:
        p = payload(ali, original.id, "Hall A")
        body, headers = signed(p)
        r = await client.post("/webhooks/platform", content=body, headers=headers)
        assert r.status_code == 202, r.text
        assert r.json()["cache_version"] == version_before + 1
        assert enqueued == [("reindex_entity_job", str(ali.tenant_id), str(ali.event_id),
                             "session", str(original.id))]  # fmt: skip
        assert (await fhir_session(rw_engine, ali)).room == "Hall A"

        # same delivery again: acknowledged, not re-applied
        dup = await client.post("/webhooks/platform", content=body, headers=headers)
        assert dup.json()["status"] == "duplicate" and len(enqueued) == 1

        stats = await reindex_entity_job({}, *enqueued[0][1:])
        assert stats["embedded"] >= 1  # the session card (+ its speakers' cards)
        card = await _q(
            rw_engine, sys_ctx,
            "SELECT content FROM embeddings WHERE entity_type = 'session' AND entity_id = :id",
            {"id": original.id},
        )  # fmt: skip
        assert "Room: Hall A" in card.content
        audit = await _q(
            rw_engine, sys_ctx,
            "SELECT count(*) FROM audit_logs WHERE action = 'webhook.session.updated' "
            "AND details->>'delivery_id' = :d", {"d": p["id"]},
        )  # fmt: skip
        assert audit[0] == 1
    finally:  # restore the seed state
        body, headers = signed(payload(ali, original.id, original.room))
        await client.post("/webhooks/platform", content=body, headers=headers)
        await reindex_entity_job({}, str(ali.tenant_id), str(ali.event_id), "session",
                                 str(original.id))  # fmt: skip
    assert (await fhir_session(rw_engine, ali)).room == original.room


async def test_unknown_room_and_unknown_fields_rejected(client, ctx_for, rw_engine, enqueued):
    ali = await ctx_for("ali", "HealthTech")
    sid = (await fhir_session(rw_engine, ali)).id
    body, headers = signed(payload(ali, sid, "The Moon"))
    assert (
        await client.post("/webhooks/platform", content=body, headers=headers)
    ).status_code == 422
    bad = payload(ali, sid, "Hall A")
    bad["data"] = {"capacity": 9999}
    body, headers = signed(bad)
    assert (
        await client.post("/webhooks/platform", content=body, headers=headers)
    ).status_code == 422
    assert not enqueued
