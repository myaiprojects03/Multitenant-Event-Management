"""Simulated external effects. Nothing leaves the machine:

* email: a fake provider that, like real ones (Postmark, SES, SendGrid), accepts an idempotency
  key; resending the same key returns the original message id instead of sending twice. The
  "inbox" is a Redis list per event, so the demo can show what was sent.
* platform_api: the event platform's "move session" call, applied to our copy of the schedule
  (as the platform's webhook would) and naturally idempotent.
* publish: an approved recap becomes an attendee-visible document; its id is derived from the
  idempotency key, so a repeat inserts nothing.

Failure injection (demo/tests only, `allow_failure_injection`): an action's payload may carry
{"simulate": {"fail_times": n}} (transient provider errors on the first n attempts) or
{"simulate": {"permanent": true}} (always rejected).
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AuditLog

INBOX_KEEP = 500
_NS = uuid.UUID("6f1c1d2e-8a51-4c8e-9c51-1b9e0f3f7a10")


class TransientSendError(Exception):
    """Worth retrying (timeouts, 429/5xx)."""


class PermanentSendError(Exception):
    """Retrying cannot help (invalid recipient, rejected payload): dead-letter at once."""


@dataclass
class SendResult:
    provider_message_id: str
    reindex: list[tuple[str, str]] = field(default_factory=list)  # (entity_type, id) after commit


class SimulatedSender:
    def __init__(self, redis: Redis):
        self.redis = redis
        self.inject = get_settings().allow_failure_injection

    async def send(self, db: AsyncSession, action: dict) -> SendResult:
        sim = (action.get("payload") or {}).get("simulate") or {}
        if self.inject and sim.get("permanent"):
            raise PermanentSendError("simulated: provider rejected the request (422)")
        if self.inject and action["attempts"] <= int(sim.get("fail_times", 0)):
            raise TransientSendError("simulated: provider unavailable (503)")
        channel = action["channel"]
        if channel == "email":
            return await self._email(action)
        if channel == "platform_api":
            return await self._move_session(db, action)
        if channel == "publish":
            return await self._publish(db, action)
        raise PermanentSendError(f"unknown channel {channel}")

    async def _email(self, action: dict) -> SendResult:
        if not action.get("recipient") or "@" not in action["recipient"]:
            raise PermanentSendError("invalid recipient address")
        key = f"sim:provider:{action['idempotency_key']}"
        new_id = "sim-" + uuid.uuid4().hex[:16]
        if await self.redis.set(key, new_id, nx=True, ex=7 * 86_400):
            inbox = f"sim:inbox:{action['event_id']}"
            message = {
                "id": new_id,
                "to": action["recipient"],
                "subject": action.get("subject"),
                "body": action.get("body"),
                "workflow_id": str(action["workflow_id"]),
                "sent_at": datetime.now(UTC).isoformat(),
            }
            await self.redis.lpush(inbox, json.dumps(message))
            await self.redis.ltrim(inbox, 0, INBOX_KEEP - 1)
            return SendResult(new_id)
        return SendResult(await self.redis.get(key))  # provider-side dedupe: same message

    async def _move_session(self, db: AsyncSession, action: dict) -> SendResult:
        p = action["payload"]
        result = await db.execute(
            text("UPDATE sessions SET room_id = :room WHERE id = :id"),
            {"room": p["to_room_id"], "id": p["session_id"]},
        )
        if result.rowcount == 0:
            raise PermanentSendError("session no longer exists")
        await _audit(db, action, "platform.session.moved", "sessions", p["session_id"], p)
        return SendResult(f"plat-{action['idempotency_key'][-12:]}", [("session", p["session_id"])])

    async def _publish(self, db: AsyncSession, action: dict) -> SendResult:
        doc_id = uuid.uuid5(_NS, action["idempotency_key"])
        await db.execute(
            text(
                "INSERT INTO documents (id, tenant_id, event_id, title, source_type, content, "
                "visibility) VALUES (:id, app_tenant(), app_event(), :title, 'text', :body, "
                "'attendee') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": doc_id, "title": action["subject"], "body": action["body"]},
        )
        await _audit(db, action, "platform.document.published", "documents", doc_id, {})
        return SendResult(f"doc-{doc_id}", [("document", str(doc_id))])


async def _audit(db, action, what, target_type, target_id, details) -> None:
    await db.execute(
        insert(AuditLog.__table__).values(
            tenant_id=action["tenant_id"],
            event_id=action["event_id"],
            actor_role="system",
            action=what,
            target_type=target_type,
            target_id=str(target_id),
            details={
                "workflow_id": str(action["workflow_id"]),
                **{k: v for k, v in details.items() if k != "simulate"},
            },
        )
    )


async def read_inbox(redis: Redis, event_id, limit: int = 50) -> list[dict]:
    return [json.loads(m) for m in await redis.lrange(f"sim:inbox:{event_id}", 0, limit - 1)]
