"""Telemetry: redaction, request ids, usage rows under RLS, the organizer API and self-test."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import telemetry as telemetry_api
from app.core.config import Settings
from app.core.context import apply_context
from app.core.db import dispose_engine
from app.core.llm import LLMGateway
from app.core.telemetry import UsageRecord, new_request_id, record_usage, redact
from app.main import app
from tests.conftest import PERSONA_EMAILS, requires_db


@pytest.fixture(autouse=True)
async def _fresh_engine():
    yield
    await dispose_engine()  # the global engine is bound to this test's event loop


def test_redact():
    raw = (
        # fake key assembled at runtime so secret scanners don't flag the repo
        "user sara@x.example.com phone +1-555-123-4567 key " + "AI" + "za" + "SyA" + "x" * 24 + " "
        "token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl password=hunter2"
    )
    out = redact(raw)
    for secret in ("sara@", "555-123", "AIzaSy", "eyJhbG", "hunter2"):
        assert secret not in out
    assert redact("x" * 900) == "x" * 500


def test_request_id_sanitized():
    assert new_request_id("abc-123") == "abc-123"
    assert new_request_id("bad id\n<script>") != "bad id\n<script>"
    assert len(new_request_id(None)) == 32


def test_request_id_header():
    with TestClient(app) as c:
        assert (
            c.get("/health", headers={"X-Request-ID": "req-42"}).headers["x-request-id"] == "req-42"
        )
        assert len(c.get("/health").headers["x-request-id"]) == 32


@requires_db
async def test_usage_rows_respect_rls(rw_engine, ctx_for):
    ali, sara, grace = (
        await ctx_for("ali", "HealthTech"),
        await ctx_for("sara"),
        await ctx_for("grace"),
    )
    route = f"test.{uuid.uuid4().hex[:8]}"
    await record_usage(sara, UsageRecord(route, 120, model="m", prompt_tokens=7, error="x@y.com"))

    async def count(ctx):
        async with rw_engine.connect() as conn, conn.begin():
            await apply_context(conn, ctx)
            return (
                await conn.execute(
                    text("SELECT count(*), max(error) FROM llm_usage_logs WHERE route_type = :r"),
                    {"r": route},
                )
            ).one()

    n, err = await count(ali)
    assert n == 1 and err == "[email]"  # organizer sees it, redacted
    assert (await count(sara))[0] == 0  # attendees can't read telemetry, even their own
    assert (await count(grace))[0] == 0  # other tenant


def _fake_gateway(n_calls: int = 1) -> LLMGateway:
    resp = SimpleNamespace(
        text='{"intent": "exact_lookup", "confidence": 0.9, "reason": "room lookup"}',
        parsed=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=40, candidates_token_count=12, thoughts_token_count=0
        ),
    )
    calls = []

    async def generate_content(*, model, contents, config):
        calls.append(model)
        return resp

    gw = LLMGateway(
        client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=generate_content))),
        settings=Settings(gemini_api_key="test"),
    )  # fmt: skip
    gw.calls = calls
    return gw


def _login(c, persona):
    r = c.post("/auth/demo-login", json={"email": PERSONA_EMAILS[persona]})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@requires_db
def test_selftest_caches_and_summary_reports_it(monkeypatch):
    gw = _fake_gateway()
    monkeypatch.setattr(telemetry_api, "get_llm", lambda: gw)
    question = f"Which room is workshop {uuid.uuid4().hex[:6]} in?"
    with TestClient(app) as c:
        h = _login(c, "ali")
        first = c.post("/telemetry/llm-selftest", json={"question": question}, headers=h).json()
        second = c.post("/telemetry/llm-selftest", json={"question": question}, headers=h).json()
        assert first["cache_hit"] is False and first["result"]["intent"] == "exact_lookup"
        assert first["prompt_tokens"] == 40
        assert second["cache_hit"] is True and second["result"] == first["result"]
        assert len(gw.calls) == 1  # the second answer never reached the LLM

        summary = c.get("/telemetry/summary?minutes=5", headers=h).json()
        route = next(r for r in summary["by_route"] if r["route_type"] == "telemetry.selftest")
        assert route["calls"] >= 2 and route["cache_hits"] >= 1
        recent = c.get("/telemetry/recent?limit=5", headers=h).json()
        assert recent and recent[0]["route_type"] == "telemetry.selftest"


@requires_db
def test_telemetry_endpoints_are_organizer_only():
    with TestClient(app) as c:
        for persona in ("sara", "maya"):
            h = _login(c, persona)
            assert c.get("/telemetry/summary", headers=h).status_code == 403
            assert c.post("/telemetry/llm-selftest", json={}, headers=h).status_code == 403
