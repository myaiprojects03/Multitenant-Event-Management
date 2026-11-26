"""GET /sessions: organizer-only program view, scoped to the token's event by RLS."""

import pytest
from fastapi.testclient import TestClient

from app.core.db import dispose_engine
from app.main import app
from tests.conftest import PERSONA_EMAILS, requires_db

pytestmark = requires_db


@pytest.fixture(autouse=True)
async def _fresh_engine():
    yield
    await dispose_engine()


def _login(c, persona, event_name=None):
    body = {"email": PERSONA_EMAILS[persona]}
    if event_name:
        users = c.get("/auth/demo-users").json()
        body["event_id"] = next(
            u["event_id"] for u in users if u["event_name"].startswith(event_name)
        )
    r = c.post("/auth/demo-login", json=body)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_sessions_are_organizer_only_and_event_scoped():
    with TestClient(app) as c:
        for persona in ("sara", "maya"):
            assert c.get("/sessions", headers=_login(c, persona)).status_code == 403

        ali = _login(c, "ali", "HealthTech")
        everything = c.get("/sessions", headers=ali).json()
        past = c.get("/sessions?when=past", headers=ali).json()
        upcoming = c.get("/sessions?when=upcoming", headers=ali).json()
        assert everything and past and upcoming
        assert {s["id"] for s in past} | {s["id"] for s in upcoming} <= {
            s["id"] for s in everything
        }

        # the planted overbooked workshop shows demand above its room's capacity
        fhir = next(
            s for s in upcoming if s["title"].startswith("Hands-on Workshop: Building FHIR")
        )
        assert fhir["bookmarks"] > fhir["room_capacity"]
        assert any(s["attended"] > 0 for s in past)

        # another tenant's organizer never sees these sessions
        grace = c.get("/sessions", headers=_login(c, "grace")).json()
        assert grace and not ({s["id"] for s in grace} & {s["id"] for s in everything})
