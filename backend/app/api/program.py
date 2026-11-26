"""Organizer view of the program: sessions with room, capacity and demand (bookmarks).

Used by the console to pick a session for the Operations Agent. Reads go through RLS, so an
organizer only ever sees their own tenant + event.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.core.security import DbDep, OrganizerDep

router = APIRouter(tags=["program"])


class ProgramSession(BaseModel):
    id: uuid.UUID
    title: str
    kind: str
    starts_at: datetime
    ends_at: datetime
    room: str | None
    room_capacity: int | None
    bookmarks: int
    attended: int


_WHEN = {
    "past": "s.ends_at < now()",
    "upcoming": "s.starts_at > now()",
    "all": "true",
}


@router.get("/sessions", response_model=list[ProgramSession])
async def list_sessions(
    _: OrganizerDep,
    db: DbDep,
    when: Annotated[Literal["past", "upcoming", "all"], Query()] = "all",
) -> list[ProgramSession]:
    rows = (
        await db.execute(
            text(
                f"""SELECT s.id, s.title, s.kind, s.starts_at, s.ends_at,
                           r.name AS room, r.capacity AS room_capacity,
                           (SELECT count(*) FROM bookmarks b WHERE b.session_id = s.id)
                             AS bookmarks,
                           (SELECT count(DISTINCT l.user_id) FROM engagement_logs l
                             WHERE l.session_id = s.id AND l.kind = 'session_check_in') AS attended
                    FROM sessions s LEFT JOIN rooms r ON r.id = s.room_id
                    WHERE {_WHEN[when]}
                    ORDER BY s.starts_at, s.title"""
            )
        )
    ).mappings()
    return [ProgramSession(**r) for r in rows]
