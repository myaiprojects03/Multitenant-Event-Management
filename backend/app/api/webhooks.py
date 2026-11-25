"""Simulated event-platform webhooks: content changed on the platform, update our copy.

Security:
* HMAC-SHA256 signature over "{timestamp}.{raw body}" with a shared secret, compared in
  constant time; timestamps older than `webhook_max_skew_s` are rejected (replay window).
* Each delivery id is processed once (Redis SET NX), so retries from the platform are safe.
* The change is applied under a `system` context pinned to the payload's tenant + event, so
  RLS still confines the write; an id from another event simply matches no row (404).
Then the event cache is invalidated at once and a background job re-embeds only that entity.
"""

import hashlib
import hmac
import time
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from sqlalchemy import insert, text

from app.core import jobs
from app.core.cache import ResponseCache
from app.core.config import get_settings
from app.core.context import RequestContext, scoped_session
from app.core.redis import get_redis
from app.models import AuditLog

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionChange(_Change):
    room: str | None = None  # room name
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    title: str | None = None
    description: str | None = None


class ExhibitorChange(_Change):
    booth: str | None = None
    category: str | None = None
    description: str | None = None


class FaqChange(_Change):
    question: str | None = None
    answer: str | None = None


CHANGES = {
    "session.updated": ("session", "sessions", SessionChange),
    "exhibitor.updated": ("exhibitor", "exhibitors", ExhibitorChange),
    "faq.updated": ("faq", "faqs", FaqChange),
}


class WebhookEvent(BaseModel):
    id: str
    type: Literal["session.updated", "exhibitor.updated", "faq.updated"]
    tenant_id: uuid.UUID
    event_id: uuid.UUID
    entity_id: uuid.UUID
    data: dict


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(body: bytes, timestamp: str | None, signature: str | None) -> None:
    s = get_settings()
    if not timestamp or not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing signature")
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad timestamp") from exc
    if age > s.webhook_max_skew_s:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale timestamp")
    expected = sign(s.webhook_secret.get_secret_value(), timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")


async def _apply(db, evt: WebhookEvent, change: _Change, table: str) -> None:
    values = change.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no changes in data")
    if "room" in values:
        room_id = (
            await db.execute(
                text("SELECT id FROM rooms WHERE lower(name) = lower(:r)"),
                {"r": values.pop("room")},
            )
        ).scalar()
        if room_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown room")
        values["room_id"] = room_id
    assignments = ", ".join(f"{col} = :{col}" for col in values)  # columns from a fixed model
    result = await db.execute(
        text(f"UPDATE {table} SET {assignments} WHERE id = :entity_id"),
        {**values, "entity_id": evt.entity_id},
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found in this event")
    await db.execute(
        insert(AuditLog.__table__).values(
            tenant_id=evt.tenant_id,
            event_id=evt.event_id,
            actor_role="system",
            action=f"webhook.{evt.type}",
            target_type=table,
            target_id=str(evt.entity_id),
            details={"delivery_id": evt.id, "fields": sorted(values)},
        )
    )


@router.post("/platform", status_code=status.HTTP_202_ACCEPTED)
async def platform_webhook(
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    x_platform_timestamp: Annotated[str | None, Header()] = None,
    x_platform_signature: Annotated[str | None, Header()] = None,
) -> dict:
    body = await request.body()
    verify(body, x_platform_timestamp, x_platform_signature)
    try:
        evt = WebhookEvent.model_validate_json(body)
        entity_type, table, model = CHANGES[evt.type]
        change = model.model_validate(evt.data)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors()) from exc

    dedupe_key = f"webhook:seen:{evt.id}"
    if not await redis.set(dedupe_key, "1", nx=True, ex=86_400):
        return {"status": "duplicate", "delivery_id": evt.id}
    ctx = RequestContext.system(evt.tenant_id, evt.event_id)
    try:
        async with scoped_session(ctx) as db:
            await _apply(db, evt, change, table)
    except Exception:
        await redis.delete(dedupe_key)  # let the platform retry
        raise
    version = await ResponseCache(redis).invalidate_event(ctx)
    job_id = await jobs.enqueue(
        "reindex_entity_job", str(evt.tenant_id), str(evt.event_id), entity_type,
        str(evt.entity_id),
    )  # fmt: skip
    return {"status": "accepted", "delivery_id": evt.id, "cache_version": version, "job_id": job_id}
