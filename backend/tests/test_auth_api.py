import uuid
from typing import Annotated

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.security import ALGORITHM, issue_token, require_roles
from app.main import app
from tests.conftest import PERSONA_EMAILS, requires_db


@app.get("/_test/organizer-only", include_in_schema=False)
async def _organizer_only(ctx: Annotated[RequestContext, Depends(require_roles("organizer"))]):
    return {"ok": True}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(client, persona: str, event_id=None) -> dict:
    body = {"email": PERSONA_EMAILS[persona]}
    if event_id:
        body["event_id"] = str(event_id)
    r = client.post("/auth/demo-login", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_cannot_issue_system_token():
    with pytest.raises(ValueError):
        issue_token(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            event_id=uuid.uuid4(),
            role="system",
            exhibitor_id=None,
        )


def test_missing_and_garbage_tokens_rejected(client):
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=auth("not-a-jwt")).status_code == 401


def test_token_signed_with_wrong_key_rejected(client):
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "eid": str(uuid.uuid4()),
            "role": "organizer",
            "exp": 4_000_000_000,
        },
        "attacker-secret",
        algorithm=ALGORITHM,
    )
    assert client.get("/me", headers=auth(forged)).status_code == 401


def test_system_role_in_token_rejected(client):
    s = get_settings()
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "eid": str(uuid.uuid4()),
            "role": "system",
            "exp": 4_000_000_000,
        },
        s.jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )
    assert client.get("/me", headers=auth(token)).status_code == 401


@requires_db
def test_demo_users_lists_personas(client):
    personas = client.get("/auth/demo-users").json()
    emails = {p["email"] for p in personas}
    assert set(PERSONA_EMAILS.values()) <= emails
    assert {p["role"] for p in personas} == {"attendee", "exhibitor", "organizer"}


@requires_db
def test_login_and_me_are_scoped(client):
    sara = login(client, "sara")
    assert sara["role"] == "attendee" and sara["exhibitor_id"] is None
    me = client.get("/me", headers=auth(sara["access_token"])).json()
    assert me["full_name"] == "Sara Ahmed"
    assert me["networking_opt_in"] is True and me["exhibitor_matching_opt_in"] is True

    maya = login(client, "maya")
    assert maya["role"] == "exhibitor" and maya["exhibitor_name"] == "MedScan"


@requires_db
def test_login_rejects_unknown_user_and_foreign_event(client):
    assert client.post("/auth/demo-login", json={"email": "nobody@example.com"}).status_code == 401
    leo_event = login(client, "leo")["event_id"]
    r = client.post(
        "/auth/demo-login", json={"email": PERSONA_EMAILS["sara"], "event_id": leo_event}
    )
    assert r.status_code == 401


@requires_db
def test_organizer_can_switch_events_within_tenant(client):
    personas = client.get("/auth/demo-users").json()
    ali_events = {
        p["event_name"]: p["event_id"] for p in personas if p["email"] == PERSONA_EMAILS["ali"]
    }
    assert len(ali_events) == 2
    for event_id in ali_events.values():
        assert login(client, "ali", event_id)["event_id"] == event_id


@requires_db
def test_require_roles(client):
    assert (
        client.get(
            "/_test/organizer-only", headers=auth(login(client, "sara")["access_token"])
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/_test/organizer-only", headers=auth(login(client, "ali")["access_token"])
        ).status_code
        == 200
    )
