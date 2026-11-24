"""Demo auth: pick a seeded user, get a JWT scoped to one tenant + event + role.

There are no passwords: this is a portfolio demo with synthetic users. Production would put a
real identity provider in front of `issue_token`; nothing downstream would change.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import CtxDep, DbDep, issue_token

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])

Session = Annotated[AsyncSession, Depends(get_session)]


class DemoPersona(BaseModel):
    email: str
    persona_label: str | None
    full_name: str
    tenant_name: str
    event_id: uuid.UUID
    event_name: str
    role: str
    exhibitor_name: str | None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    event_id: uuid.UUID | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: uuid.UUID
    full_name: str
    tenant_id: uuid.UUID
    tenant_name: str
    event_id: uuid.UUID
    event_name: str
    role: str
    exhibitor_id: uuid.UUID | None
    exhibitor_name: str | None


@router.get("/demo-users", response_model=list[DemoPersona])
async def demo_users(session: Session) -> list[DemoPersona]:
    rows = (await session.execute(text("SELECT * FROM auth_demo_personas()"))).mappings()
    return [DemoPersona(**r) for r in rows]


@router.post("/demo-login", response_model=LoginResponse)
async def demo_login(body: LoginRequest, session: Session) -> LoginResponse:
    rows = (
        (await session.execute(text("SELECT * FROM auth_memberships(:e)"), {"e": body.email}))
        .mappings()
        .all()
    )
    if body.event_id:
        rows = [r for r in rows if r["event_id"] == body.event_id]
    if not rows:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user or no access to event")
    m = rows[0]  # a user holds one role per event; without event_id, take their first event
    token, ttl = issue_token(
        user_id=m["user_id"],
        tenant_id=m["tenant_id"],
        event_id=m["event_id"],
        role=m["role"],
        exhibitor_id=m["exhibitor_id"],
    )
    return LoginResponse(access_token=token, expires_in=ttl, **m)


class Me(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    tenant_id: uuid.UUID
    event_id: uuid.UUID
    event_name: str
    role: str
    exhibitor_id: uuid.UUID | None
    networking_opt_in: bool | None
    exhibitor_matching_opt_in: bool | None


@me_router.get("/me", response_model=Me)
async def me(ctx: CtxDep, db: DbDep) -> Me:
    # read through RLS: proves the request is scoped to the token
    row = (
        (
            await db.execute(
                text(
                    """SELECT u.id AS user_id, u.full_name, u.email, e.name AS event_name,
                              p.networking_opt_in, p.exhibitor_matching_opt_in
                       FROM users u
                       JOIN events e ON e.id = app_event()
                       LEFT JOIN attendee_profiles p
                         ON p.user_id = u.id AND p.event_id = app_event()
                       WHERE u.id = app_user()"""
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not visible in this scope")
    return Me(
        tenant_id=ctx.tenant_id,
        event_id=ctx.event_id,
        role=ctx.role,
        exhibitor_id=ctx.exhibitor_id,
        **row,
    )
