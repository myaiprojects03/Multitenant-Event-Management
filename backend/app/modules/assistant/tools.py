"""Bounded actions the assistant may take. Permissions are checked here in code, and every
query/write runs in the caller's RLS-scoped session with ids taken from RequestContext, never
from the model. The model only supplies a search phrase.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models import AuditLog
from app.modules.assistant.retrieval import lookup_sessions


@dataclass
class ToolResult:
    message: str
    data: Any = None
    sources: list[dict] = field(default_factory=list)  # {entity_type, entity_id, title, snippet}


def _deny(what: str) -> ToolResult:
    return ToolResult(f"Sorry, {what} is only available to attendees.")


async def _audit(db: AsyncSession, ctx: RequestContext, action: str, target_id) -> None:
    await db.execute(
        insert(AuditLog.__table__).values(
            tenant_id=ctx.tenant_id,
            event_id=ctx.event_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=action,
            target_type="session",
            target_id=str(target_id),
            details={"via": "assistant"},
        )
    )


async def bookmark_session(db: AsyncSession, ctx: RequestContext, term: str | None) -> ToolResult:
    if ctx.role != "attendee":
        return _deny("bookmarking sessions")
    if not term:
        return ToolResult("Which session would you like me to bookmark?")
    matches = await lookup_sessions(db, term)
    if not matches:
        return ToolResult(f"I couldn't find a session matching '{term}'.")
    best = matches[0]
    # ambiguous: a second match nearly as good as the first
    if len(matches) > 1 and matches[1].score > best.score - 0.1:
        options = "; ".join(m.title for m in matches)
        return ToolResult(f"Which one did you mean: {options}?", data=[m.title for m in matches])
    source = {"entity_type": "session", "entity_id": best.entity_id, "title": best.title,
              "snippet": best.text}  # fmt: skip
    already = (
        await db.execute(
            text("SELECT 1 FROM bookmarks WHERE user_id = app_user() AND session_id = :s"),
            {"s": best.entity_id},
        )
    ).first()
    if already:
        return ToolResult(f"'{best.title}' is already in your bookmarks.", sources=[source])
    await db.execute(
        text(
            "INSERT INTO bookmarks (tenant_id, event_id, user_id, session_id) "
            "VALUES (app_tenant(), app_event(), app_user(), :s)"
        ),
        {"s": best.entity_id},
    )
    await _audit(db, ctx, "bookmark.created", best.entity_id)
    return ToolResult(f"Bookmarked '{best.title}'. {best.text}", sources=[source])


async def list_bookmarks(db: AsyncSession, ctx: RequestContext) -> ToolResult:
    if ctx.role != "attendee":
        return _deny("bookmarks")
    rows = (
        await db.execute(
            text(
                """SELECT s.id, s.title, s.starts_at, r.name AS room
                   FROM bookmarks b JOIN sessions s ON s.id = b.session_id
                   LEFT JOIN rooms r ON r.id = s.room_id
                   WHERE b.user_id = app_user() ORDER BY s.starts_at"""
            )
        )
    ).mappings()
    items = [
        {
            "session_id": str(r["id"]),
            "title": r["title"],
            "room": r["room"],
            "starts_at": r["starts_at"].isoformat(),
        }  # fmt: skip
        for r in rows
    ]
    if not items:
        return ToolResult("You have no bookmarked sessions yet.", data=[])
    lines = "; ".join(
        f"{i['title']} ({i['room']}, {i['starts_at'][5:10]} {i['starts_at'][11:16]} UTC)"
        for i in items
    )
    return ToolResult(f"You have {len(items)} bookmarked sessions: {lines}.", data=items)


async def filter_exhibitors(db: AsyncSession, ctx: RequestContext, term: str | None) -> ToolResult:
    if not term:
        cats = (await db.execute(text("SELECT DISTINCT category FROM exhibitors ORDER BY 1"))).all()
        return ToolResult(
            "Exhibitor categories: " + ", ".join(c[0] for c in cats) + ".",
            data=[c[0] for c in cats],
        )
    rows = (
        await db.execute(
            text(
                """SELECT id, name, booth, category,
                          (category ILIKE '%' || :t || '%'
                           OR word_similarity(lower(:t), lower(category)) > 0.6) AS in_category
                   FROM exhibitors
                   WHERE category ILIKE '%' || :t || '%'
                      OR EXISTS (SELECT 1 FROM unnest(tags) tag WHERE tag ILIKE '%' || :t || '%')
                      OR word_similarity(lower(:t), lower(category)) > 0.6
                   ORDER BY in_category DESC, name LIMIT 25"""
            ),
            {"t": term},
        )
    ).mappings()
    items = [{"exhibitor_id": str(r["id"]), "name": r["name"], "booth": r["booth"],
              "category": r["category"], "in_category": r["in_category"]}
             for r in rows]  # fmt: skip
    if not items:
        return ToolResult(f"No exhibitors match '{term}'.", data=[])
    core = [i for i in items if i["in_category"]]
    related = [i for i in items if not i["in_category"]]
    msg = ""
    if core:
        msg = (
            f"{core[0]['category']} exhibitors: "
            + "; ".join(f"{i['name']} (booth {i['booth']})" for i in core)
            + "."
        )
    if related:
        msg += (
            (" Also working on " if core else "Exhibitors working on ")
            + f"'{term}': "
            + ("; ".join(f"{i['name']} (booth {i['booth']})" for i in related) + ".")
        )
    return ToolResult(msg.strip(), data=items)
