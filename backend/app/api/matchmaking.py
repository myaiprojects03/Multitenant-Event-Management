"""Matchmaking API: recommendations + feedback, peer connections, exhibitor meetings,
badge scans, and the attendee's two consent switches.

Ids of the caller always come from the token. Consent-crossing steps (exhibitor -> prospect,
accepting a meeting, badge scans) go through SECURITY DEFINER SQL functions that re-check the
caller's context and create leads only at the consent moment.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.llm import get_llm
from app.core.redis import get_redis
from app.core.security import CtxDep, DbDep, require_roles
from app.models import AuditLog
from app.modules.matchmaking.service import (
    MatchmakingService,
    NotAllowed,
    RecommendationList,
    RecType,
    category_of,
)

router = APIRouter(tags=["matchmaking"])
AttendeeDep = Annotated[RequestContext, Depends(require_roles("attendee"))]
ExhibitorDep = Annotated[RequestContext, Depends(require_roles("exhibitor"))]
RedisDep = Annotated[Redis, Depends(get_redis)]


def _sql_error(exc: DBAPIError) -> HTTPException:
    msg = str(getattr(exc, "orig", exc)).splitlines()[0].split(": ", 1)[-1]
    code = (
        status.HTTP_404_NOT_FOUND
        if "not found" in msg or "not available" in msg
        else (status.HTTP_409_CONFLICT if "already" in msg else status.HTTP_400_BAD_REQUEST)
    )
    return HTTPException(code, msg)


async def _audit(db: AsyncSession, ctx: RequestContext, action: str, target_type: str, target):
    await db.execute(
        insert(AuditLog.__table__).values(
            tenant_id=ctx.tenant_id,
            event_id=ctx.event_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=action,
            target_type=target_type,
            target_id=str(target),
        )
    )


# --- recommendations ----------------------------------------------------------------------------


@router.get("/recommendations", response_model=RecommendationList)
async def recommendations(
    ctx: CtxDep,
    db: DbDep,
    redis: RedisDep,
    type: Annotated[RecType, Query()] = "peers",  # noqa: A002 - matches the spec's ?type=
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    phrase: bool = True,
) -> RecommendationList:
    try:
        return await MatchmakingService(get_llm(), redis).recommend(db, ctx, type, limit, phrase)
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


class Feedback(BaseModel):
    target_type: Literal["peer", "session", "speaker", "exhibitor", "prospect"]
    target_id: uuid.UUID
    action: Literal["click", "save", "dismiss"]


@router.post("/recommendations/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(body: Feedback, ctx: CtxDep, db: DbDep) -> None:
    """Clicks/saves boost a category, dismissals hide the item for good and dampen its
    category. Saving a session also bookmarks it."""
    if (body.target_type == "prospect") != (ctx.role == "exhibitor"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "target type not available to your role")
    if body.action == "save" and body.target_type == "session":
        await db.execute(
            text(
                "INSERT INTO bookmarks (tenant_id, event_id, user_id, session_id) "
                "SELECT app_tenant(), app_event(), app_user(), id FROM sessions WHERE id = :s "
                "ON CONFLICT (user_id, session_id) DO NOTHING"
            ),
            {"s": body.target_id},
        )
    await db.execute(
        text(
            "INSERT INTO recommendation_events (tenant_id, event_id, user_id, target_type, "
            "target_id, kind, category) VALUES (app_tenant(), app_event(), app_user(), :t, :id, "
            ":k, :c)"
        ),
        {
            "t": body.target_type,
            "id": body.target_id,
            "k": body.action,
            "c": await category_of(db, body.target_type, body.target_id),
        },
    )


# --- consent ------------------------------------------------------------------------------------


class Consent(BaseModel):
    networking_opt_in: bool | None = None
    exhibitor_matching_opt_in: bool | None = None


@router.put("/me/consent", response_model=Consent)
async def set_consent(body: Consent, ctx: AttendeeDep, db: DbDep) -> Consent:
    """The attendee's two independent opt-ins. Opting out removes them from peer matches /
    exhibitor prospect lists immediately (both are filtered in SQL at query time)."""
    values = body.model_dump(exclude_none=True)
    if values:
        sets = ", ".join(f"{k} = :{k}" for k in values)
        await db.execute(
            text(
                f"UPDATE attendee_profiles SET {sets}, updated_at = now() "
                "WHERE user_id = app_user()"
            ),
            values,
        )
        await _audit(db, ctx, "consent.updated", "attendee_profile", ctx.user_id)
    row = (
        (
            await db.execute(
                text(
                    "SELECT networking_opt_in, exhibitor_matching_opt_in FROM attendee_profiles "
                    "WHERE user_id = app_user()"
                )
            )
        )
        .mappings()
        .one()
    )
    return Consent(**row)


# --- peer connections ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    user_id: uuid.UUID
    message: str | None = Field(default=None, max_length=500)


class Decision(BaseModel):
    accept: bool


@router.post("/connections", status_code=status.HTTP_201_CREATED)
async def create_connection(body: ConnectionCreate, ctx: AttendeeDep, db: DbDep) -> dict:
    mine = (
        await db.execute(
            text("SELECT networking_opt_in FROM attendee_profiles WHERE user_id = app_user()")
        )
    ).scalar()
    if not mine:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "turn on networking to connect with peers")
    # the target's profile is only visible (RLS) if they opted in to networking
    target = (
        await db.execute(
            text("SELECT id FROM attendee_profiles WHERE user_id = :u AND user_id <> app_user()"),
            {"u": body.user_id},
        )
    ).scalar()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attendee not available for networking")
    existing = (
        await db.execute(
            text("""SELECT id, status FROM connections
                    WHERE (requester_id = app_user() AND addressee_id = :u)
                       OR (addressee_id = app_user() AND requester_id = :u)"""),
            {"u": body.user_id},
        )
    ).first()
    if existing:
        return {"id": existing.id, "status": existing.status, "created": False}
    new_id = (
        await db.execute(
            text("""INSERT INTO connections (tenant_id, event_id, requester_id, addressee_id,
                                             status, message)
                    VALUES (app_tenant(), app_event(), app_user(), :u, 'pending', :m)
                    RETURNING id"""),
            {"u": body.user_id, "m": body.message},
        )
    ).scalar()
    await db.execute(
        text(
            "INSERT INTO recommendation_events (tenant_id, event_id, user_id, target_type, "
            "target_id, kind, category) SELECT app_tenant(), app_event(), app_user(), 'peer', "
            "id, 'connect', persona_type FROM attendee_profiles WHERE id = :p"
        ),
        {"p": target},
    )
    return {"id": new_id, "status": "pending", "created": True}


@router.post("/connections/{connection_id}/respond")
async def respond_connection(
    connection_id: uuid.UUID, body: Decision, ctx: AttendeeDep, db: DbDep
) -> dict:
    res = await db.execute(
        text("""UPDATE connections SET status = :s, responded_at = now()
                WHERE id = :id AND addressee_id = app_user() AND status = 'pending'"""),
        {"s": "accepted" if body.accept else "declined", "id": connection_id},
    )
    if res.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pending request for you with that id")
    return {"id": connection_id, "status": "accepted" if body.accept else "declined"}


@router.get("/connections")
async def list_connections(ctx: AttendeeDep, db: DbDep) -> list[dict]:
    rows = (
        await db.execute(
            text("""SELECT c.id, c.status, c.created_at,
                           CASE WHEN c.requester_id = app_user() THEN 'outgoing' ELSE 'incoming'
                           END AS direction,
                           other.id AS other_user_id, p.display_name, p.job_title,
                           u.email AS contact_email   -- visible only once accepted (RLS)
                    FROM connections c
                    CROSS JOIN LATERAL (SELECT CASE WHEN c.requester_id = app_user()
                                        THEN c.addressee_id ELSE c.requester_id END AS id) other
                    LEFT JOIN attendee_profiles p ON p.user_id = other.id
                    LEFT JOIN users u ON u.id = other.id AND c.status = 'accepted'
                    ORDER BY c.created_at DESC""")
        )
    ).mappings()
    return [dict(r) for r in rows]


# --- meetings with exhibitors ---------------------------------------------------------------------


class MeetingCreate(BaseModel):
    exhibitor_id: uuid.UUID | None = None  # attendee -> exhibitor
    profile_id: uuid.UUID | None = None  # exhibitor -> anonymized prospect
    message: str | None = Field(default=None, max_length=500)


@router.post("/meetings", status_code=status.HTTP_201_CREATED)
async def create_meeting(body: MeetingCreate, ctx: CtxDep, db: DbDep) -> dict:
    try:
        if ctx.role == "attendee" and body.exhibitor_id:
            existing = (
                await db.execute(
                    text("""SELECT id FROM meeting_requests WHERE attendee_user_id = app_user()
                            AND exhibitor_id = :x AND status IN ('pending', 'accepted')"""),
                    {"x": body.exhibitor_id},
                )
            ).scalar()
            if existing:
                return {"id": existing, "created": False}
            new_id = (
                await db.execute(
                    text("""INSERT INTO meeting_requests (tenant_id, event_id, attendee_user_id,
                                exhibitor_id, initiated_by, status, message)
                            SELECT app_tenant(), app_event(), app_user(), id, 'attendee',
                                   'pending', :m FROM exhibitors WHERE id = :x
                            RETURNING id"""),
                    {"x": body.exhibitor_id, "m": body.message},
                )
            ).scalar()
            if new_id is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "exhibitor not found")
            return {"id": new_id, "created": True}
        if ctx.role == "exhibitor" and body.profile_id:
            new_id = (
                await db.execute(
                    text("SELECT request_meeting_with_prospect(:p, :m)"),
                    {"p": body.profile_id, "m": body.message or ""},
                )
            ).scalar()
            return {"id": new_id, "created": True}
    except DBAPIError as exc:
        raise _sql_error(exc) from exc
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "attendees send exhibitor_id; exhibitors send profile_id",
    )


@router.post("/meetings/{meeting_id}/respond")
async def respond_meeting(meeting_id: uuid.UUID, body: Decision, ctx: CtxDep, db: DbDep) -> dict:
    """The receiving party decides. Acceptance is the consent moment: the lead is created."""
    try:
        row = (
            (
                await db.execute(
                    text("SELECT * FROM respond_meeting(:id, :a)"),
                    {"id": meeting_id, "a": body.accept},
                )
            )
            .mappings()
            .one()
        )
    except DBAPIError as exc:
        raise _sql_error(exc) from exc
    return {"id": meeting_id, "status": row["status"], "lead_id": row["lead_id"]}


@router.get("/meetings")
async def list_meetings(ctx: CtxDep, db: DbDep) -> list[dict]:
    if ctx.role not in ("attendee", "exhibitor"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "attendees and exhibitors only")
    rows = (
        await db.execute(
            text("""SELECT m.id, m.status, m.initiated_by, m.created_at, m.message,
                           x.name AS exhibitor_name, x.booth,
                           u.full_name AS attendee_name   -- exhibitors see it only for leads
                    FROM meeting_requests m
                    JOIN exhibitors x ON x.id = m.exhibitor_id
                    LEFT JOIN users u ON u.id = m.attendee_user_id
                    ORDER BY m.created_at DESC""")
        )
    ).mappings()
    out = []
    for r in rows:
        d = dict(r)
        if ctx.role == "exhibitor" and d["attendee_name"] is None:
            d["attendee_name"] = "Anonymous prospect"
        out.append(d)
    return out


# --- badge scan ---------------------------------------------------------------------------------


class BadgeScan(BaseModel):
    attendee_user_id: uuid.UUID  # what the badge QR code encodes


@router.post("/leads/badge-scan", status_code=status.HTTP_201_CREATED)
async def badge_scan(body: BadgeScan, ctx: ExhibitorDep, db: DbDep) -> dict:
    """Scanning a badge is the attendee's in-person consent to share contact details."""
    try:
        lead_id = (
            await db.execute(text("SELECT badge_scan(:u)"), {"u": body.attendee_user_id})
        ).scalar()
    except DBAPIError as exc:
        raise _sql_error(exc) from exc
    return {"lead_id": lead_id}
