"""Workflow engine: trigger -> agent run -> approval chain -> materialize -> deliver.

Guarantees and where they come from
* Nothing executes without approval: actions exist only in outbound_actions, and RLS on that
  table admits a row only when every approval of its workflow is approved (migration 0005).
* The owner approves: the approval chain starts with the policy's approver role; exhibitor
  steps are bound to the owning exhibitor. Bulk (> bulk_threshold recipients) or flagged drafts
  add an organizer step. The chain is recomputed from the final draft at every decision.
* Double approval sends once: the workflow row is locked (FOR UPDATE) for each decision, a
  decided approval cannot be decided again, the final approval gets a unique idempotency key,
  every action's key derives from it (unique index, ON CONFLICT DO NOTHING), and the simulated
  provider dedupes on that key as well.
* Failures: transient errors retry with exponential backoff; after `delivery_max_attempts` (or a
  permanent error) the action is dead-lettered and later stages are cancelled.

Engine bookkeeping (approvals, actions, delivery) runs under a `system` context; the agent
itself and the caller's decision checks run under the caller's / owner's context.
"""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext, apply_context
from app.core.llm import LLMGateway
from app.models import AuditLog
from app.modules.workflows import agents as ag
from app.modules.workflows.policies import PolicyMissing, approval_chain, load_policy
from app.modules.workflows.sender import PermanentSendError, SimulatedSender, TransientSendError
from app.modules.workflows.states import check_transition

log = structlog.get_logger(__name__)

# workflow kind -> role allowed to trigger it
KIND_ROLES = {
    "lead_follow_up": "exhibitor",
    "session_summary": "organizer",
    "schedule_change": "organizer",
}


class WorkflowError(Exception):
    status_code = 400


class NotFound(WorkflowError):
    status_code = 404


class NotAllowed(WorkflowError):
    status_code = 403


class Conflict(WorkflowError):
    status_code = 409


@dataclass
class DeliveryReport:
    status: str
    sent: int = 0
    retrying: int = 0
    dead_letter: int = 0
    cancelled: int = 0
    next_in_s: float | None = None
    reindex: list[tuple[str, str]] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


@asynccontextmanager
async def as_system(db: AsyncSession, ctx: RequestContext) -> AsyncIterator[None]:
    """Run engine bookkeeping as `system` in the same transaction, then restore ctx."""
    await apply_context(db, RequestContext.system(ctx.tenant_id, ctx.event_id))
    try:
        yield
    finally:
        await apply_context(db, ctx)


async def _one(db: AsyncSession, sql: str, params: dict) -> dict | None:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def _lock(db: AsyncSession, wf_id: uuid.UUID) -> dict | None:
    return await _one(db, "SELECT * FROM workflows WHERE id = :id FOR UPDATE", {"id": wf_id})


def _entry(frm: str | None, to: str | None, ctx: RequestContext, note: str | None) -> str:
    return json.dumps(
        [
            {
                "at": _now().isoformat(),
                "from": frm,
                "to": to,
                "actor_role": ctx.role,
                "actor_user_id": str(ctx.user_id) if ctx.user_id else None,
                "note": note,
            }
        ]
    )


async def _move(
    db: AsyncSession,
    wf: dict,
    to: str,
    ctx: RequestContext,
    *,
    note: str | None = None,
    output: dict | None = None,
    error: str | None = None,
) -> None:
    check_transition(wf["status"], to)
    await db.execute(
        text(
            "UPDATE workflows SET status = :to, updated_at = now(), "
            "history = history || CAST(:entry AS jsonb), "
            "output = coalesce(CAST(:output AS jsonb), output), error = coalesce(:error, error) "
            "WHERE id = :id"
        ),
        {
            "to": to,
            "entry": _entry(wf["status"], to, ctx, note),
            "output": json.dumps(_jsonable(output)) if output is not None else None,
            "error": error,
            "id": wf["id"],
        },
    )
    await _audit(db, ctx, f"workflow.{to}", wf["id"], {"from": wf["status"], "note": note})
    wf["status"] = to


async def _note(db: AsyncSession, wf: dict, ctx: RequestContext, note: str) -> None:
    await db.execute(
        text(
            "UPDATE workflows SET updated_at = now(), history = history || CAST(:e AS jsonb) "
            "WHERE id = :id"
        ),
        {"e": _entry(wf["status"], wf["status"], ctx, note), "id": wf["id"]},
    )


async def _audit(db: AsyncSession, ctx: RequestContext, action: str, wf_id, details: dict) -> None:
    await db.execute(
        insert(AuditLog.__table__).values(
            tenant_id=ctx.tenant_id,
            event_id=ctx.event_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=action,
            target_type="workflow",
            target_id=str(wf_id),
            details=_jsonable(details),
        )
    )


def owner_context(wf: dict) -> RequestContext:
    """The agent runs with its owner's rights, rebuilt from the row the owner created."""
    role = "exhibitor" if wf["owner_exhibitor_id"] else "organizer"
    return RequestContext(
        tenant_id=wf["tenant_id"],
        event_id=wf["event_id"],
        role=role,
        user_id=wf["owner_user_id"],
        exhibitor_id=wf["owner_exhibitor_id"],
    )


class WorkflowEngine:
    def __init__(self, llm: LLMGateway | None, sender: SimulatedSender | None = None):
        self.llm = llm
        self.sender = sender
        self.settings = get_settings()

    # --- trigger -------------------------------------------------------------------------------

    async def trigger(
        self, db: AsyncSession, ctx: RequestContext, kind: str, params: dict
    ) -> uuid.UUID:
        role = KIND_ROLES.get(kind)
        if role is None:
            raise WorkflowError(f"unknown workflow kind {kind}")
        if ctx.role != role:
            raise NotAllowed(f"{kind} can only be started by an {role}")
        params = _jsonable(params)
        if not self.settings.allow_failure_injection:
            params.pop("simulate", None)
        wf_id = (
            await db.execute(
                text(
                    "INSERT INTO workflows (tenant_id, event_id, kind, status, owner_user_id, "
                    "owner_exhibitor_id, input, history) VALUES (app_tenant(), app_event(), :k, "
                    "'pending', app_user(), app_exhibitor(), CAST(:input AS jsonb), "
                    "CAST(:h AS jsonb)) RETURNING id"
                ),
                {
                    "k": kind,
                    "input": json.dumps(params),
                    "h": _entry(None, "pending", ctx, "triggered"),
                },
            )
        ).scalar_one()
        await _audit(db, ctx, "workflow.triggered", wf_id, {"kind": kind})
        return wf_id

    # --- agent step ----------------------------------------------------------------------------

    async def run(self, db: AsyncSession, ctx: RequestContext, wf_id: uuid.UUID) -> str:
        """Run the agent under the owner's ctx (db must be scoped to it). Idempotent."""
        wf = await _lock(db, wf_id)
        if wf is None:
            raise NotFound("workflow not found")
        if wf["status"] != "pending":
            return wf["status"]
        await _move(db, wf, "running", ctx)
        try:
            async with db.begin_nested():  # a failing agent rolls back its own writes only
                result = await self._agent(db, ctx, wf["kind"], wf["input"])
                policy = None if result.nothing_to_do else await load_policy(db, result.action_type)
        except (ag.AgentError, PolicyMissing) as exc:
            await _move(db, wf, "failed", ctx, error=str(exc))
            return wf["status"]
        except Exception as exc:  # never leave a workflow stuck in `running`
            log.exception("workflow_agent_crashed", workflow_id=str(wf_id))
            await _move(db, wf, "failed", ctx, error=f"agent error: {type(exc).__name__}")
            return wf["status"]

        out = {**result.output, "action_type": result.action_type, "used_llm": result.used_llm}
        if result.nothing_to_do:
            await _move(db, wf, "completed", ctx, note=result.nothing_to_do, output=out)
            return wf["status"]
        flags = ag.draft_flags(result.action_type, result.draft)
        chain = approval_chain(policy, result.n_recipients, bool(flags))
        out |= {"approval_chain": chain, "flags": flags, "n_recipients": result.n_recipients}
        async with as_system(db, ctx):
            await self._add_approval(db, wf, result.action_type, 0, chain[0], result.draft, policy)
        await _move(db, wf, "awaiting_approval", ctx, note=f"waiting for {chain[0]}", output=out)
        return wf["status"]

    async def _agent(self, db, ctx, kind: str, params: dict) -> ag.AgentResult:
        if kind == "lead_follow_up":
            return await ag.LeadQualificationAgent(self.llm).run(db, ctx, params)
        ops = ag.OperationsAgent(self.llm)
        params = {**params, "session_id": uuid.UUID(params["session_id"])}
        if kind == "session_summary":
            return await ops.session_summary(db, ctx, params)
        return await ops.schedule_change(db, ctx, params)

    async def _add_approval(self, db, wf, action_type, level, role, draft, policy) -> None:
        await db.execute(
            text(
                "INSERT INTO approvals (tenant_id, event_id, workflow_id, action_type, status, "
                "required_role, approver_exhibitor_id, escalation_level, draft, expires_at) "
                "VALUES (app_tenant(), app_event(), :wf, :a, 'pending', :role, :ex, :lvl, "
                "CAST(:draft AS jsonb), :exp)"
            ),
            {
                "wf": wf["id"],
                "a": action_type,
                "role": role,
                "ex": wf["owner_exhibitor_id"] if role == "exhibitor" else None,
                "lvl": level,
                "draft": json.dumps(_jsonable(draft)),
                "exp": _now() + timedelta(hours=policy.expiry_hours),
            },
        )

    # --- human decision ------------------------------------------------------------------------

    async def decide(
        self,
        db: AsyncSession,
        ctx: RequestContext,
        wf_id: uuid.UUID,
        decision: str,
        reason: str | None = None,
        edits: dict | None = None,
    ) -> str:
        wf = await _lock(db, wf_id)  # under the caller's RLS: unseen workflows are 404
        if wf is None:
            raise NotFound("workflow not found")
        if wf["status"] != "awaiting_approval":
            raise Conflict(f"workflow is {wf['status']}; there is nothing to decide")
        async with as_system(db, ctx):
            appr = await _one(
                db,
                "SELECT * FROM approvals WHERE workflow_id = :wf AND status = 'pending' "
                "ORDER BY escalation_level DESC LIMIT 1 FOR UPDATE",
                {"wf": wf_id},
            )
        if appr is None:
            raise Conflict("no pending approval")
        if ctx.role != appr["required_role"] or (
            ctx.role == "exhibitor" and appr["approver_exhibitor_id"] != ctx.exhibitor_id
        ):
            raise NotAllowed(f"this step needs approval by the {appr['required_role']}")
        if appr["expires_at"] and appr["expires_at"] < _now():
            await self.expire(db, ctx, wf_id)  # commit the expiry; the caller sees `expired`
            return "expired"

        draft = appr["draft"]
        if edits:
            if appr["escalation_level"] > 0:
                raise ag.EditRejected("escalation reviewers approve or reject; the owner edits")
            draft = ag.apply_edits(appr["action_type"], draft, edits)

        if decision == "reject":
            if not (reason or "").strip():
                raise WorkflowError("a reason is required to reject")
            async with as_system(db, ctx):
                await self._close_approval(db, appr, "rejected", ctx, reason, draft)
            await _move(db, wf, "rejected", ctx, note=reason, error=f"rejected: {reason}")
            return wf["status"]
        if decision != "approve":
            raise WorkflowError("decision must be approve or reject")

        policy = await load_policy(db, appr["action_type"])
        flags = ag.draft_flags(appr["action_type"], draft)
        n = ag.draft_recipients(appr["action_type"], draft)
        chain = approval_chain(policy, n, bool(flags))
        level = appr["escalation_level"]
        async with as_system(db, ctx):
            await self._close_approval(db, appr, "approved", ctx, reason, draft)
            if level + 1 < len(chain):
                await self._add_approval(
                    db, wf, appr["action_type"], level + 1, chain[level + 1], draft, policy
                )
                why = (
                    "flagged: " + "; ".join(flags)
                    if flags
                    else f"bulk: {n} recipients > {policy.bulk_threshold}"
                )
                await _note(
                    db, wf, ctx, f"approved by {ctx.role}; escalated to {chain[level + 1]} ({why})"
                )
                return wf["status"]
            key = (
                f"wf:{wf_id}:"
                + hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()[:20]
            )
            await db.execute(
                text("UPDATE approvals SET idempotency_key = :k WHERE id = :id"),
                {"k": key, "id": appr["id"]},
            )
        await _move(db, wf, "approved", ctx, note=reason)
        async with as_system(db, ctx):
            n_actions = await self._materialize(db, wf, appr, key, draft)
        await _move(db, wf, "executing", ctx, note=f"{n_actions} action(s) queued")
        return wf["status"]

    async def _close_approval(self, db, appr, status, ctx, reason, draft) -> None:
        await db.execute(
            text(
                "UPDATE approvals SET status = :s, decided_by = :u, decided_at = now(), "
                "reason = :r, draft = CAST(:d AS jsonb) WHERE id = :id AND status = 'pending'"
            ),
            {
                "s": status,
                "u": ctx.user_id,
                "r": reason,
                "d": json.dumps(_jsonable(draft)),
                "id": appr["id"],
            },
        )

    async def _materialize(self, db, wf: dict, appr: dict, key: str, draft: dict) -> int:
        """Turn the approved draft into outbox rows. Recipients are re-resolved from the DB
        here, never taken from the (editable) draft."""
        sim = wf["input"].get("simulate") if self.settings.allow_failure_injection else None
        base = {"wf": wf["id"], "appr": appr["id"], "ex": wf["owner_exhibitor_id"]}
        rows: list[dict] = []

        def add(
            stage,
            channel,
            suffix,
            payload,
            recipient_user_id=None,
            recipient=None,
            subject=None,
            body=None,
        ):
            if sim:
                payload = {**payload, "simulate": sim}
            rows.append(
                base
                | {
                    "key": f"{key}:{suffix}",
                    "stage": stage,
                    "channel": channel,
                    "ruid": recipient_user_id,
                    "rcpt": recipient,
                    "subj": subject,
                    "body": body,
                    "payload": json.dumps(_jsonable(payload)),
                }
            )

        action = appr["action_type"]
        if action == "exhibitor_follow_up_email":
            for m in draft["messages"]:
                who = await _one(
                    db,
                    "SELECT u.id, u.email FROM leads l JOIN users u ON u.id = l.attendee_user_id "
                    "WHERE l.id = :lead AND l.exhibitor_id = :ex",
                    {"lead": uuid.UUID(m["lead_id"]), "ex": wf["owner_exhibitor_id"]},
                )
                if who is None:  # lead vanished: never email someone who is not a lead
                    continue
                add(
                    0,
                    "email",
                    f"email:{who['id']}",
                    {"lead_id": m["lead_id"]},
                    who["id"],
                    who["email"],
                    m["subject"],
                    m["body"],
                )
        elif action == "schedule_change":
            c = draft["change"]
            add(
                0,
                "platform_api",
                "move",
                {"session_id": c["session_id"], "to_room_id": c["to_room_id"]},
            )
            people = (
                await db.execute(
                    text(
                        "SELECT u.id, u.email FROM bookmarks b JOIN users u ON u.id = b.user_id "
                        "WHERE b.session_id = :s ORDER BY u.id"
                    ),
                    {"s": uuid.UUID(c["session_id"])},
                )
            ).mappings()
            a = draft["announcement"]
            for p in people:
                add(
                    1,
                    "email",
                    f"announce:{p['id']}",
                    {"session_id": c["session_id"]},
                    p["id"],
                    p["email"],
                    a["subject"],
                    a["body"],
                )
        elif action == "session_summary_publish":
            d = draft["document"]
            add(
                0,
                "publish",
                "publish",
                {"session_id": draft["session_id"]},
                None,
                None,
                d["title"],
                d["body"],
            )
        if rows:
            await db.execute(
                text(
                    "INSERT INTO outbound_actions (tenant_id, event_id, workflow_id, approval_id, "
                    "owner_exhibitor_id, idempotency_key, stage, channel, recipient_user_id, "
                    "recipient, subject, body, payload) VALUES (app_tenant(), app_event(), :wf, "
                    ":appr, :ex, :key, :stage, :channel, :ruid, :rcpt, :subj, :body, "
                    "CAST(:payload AS jsonb)) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                rows,
            )
        return len(rows)

    # --- delivery ------------------------------------------------------------------------------

    async def deliver(self, db: AsyncSession, ctx: RequestContext, wf_id: uuid.UUID):
        """One delivery pass (db scoped to `system`). Sends what is due, stage by stage."""
        assert self.sender is not None, "deliver needs a sender"
        wf = await _lock(db, wf_id)
        if wf is None:
            raise NotFound("workflow not found")
        report = DeliveryReport(status=wf["status"])
        if wf["status"] != "executing":
            return report
        s = self.settings
        acts = [
            dict(r)
            for r in (
                await db.execute(
                    text(
                        "SELECT * FROM outbound_actions WHERE workflow_id = :wf "
                        "ORDER BY stage, created_at, idempotency_key"
                    ),
                    {"wf": wf_id},
                )
            ).mappings()
        ]
        for stage in sorted({a["stage"] for a in acts}):
            in_stage = [a for a in acts if a["stage"] == stage]
            for a in in_stage:
                if a["status"] not in ("queued", "retrying") or a["next_attempt_at"] > _now():
                    continue
                a["attempts"] += 1
                try:
                    res = await self.sender.send(db, a)
                except (TransientSendError, PermanentSendError) as exc:
                    dead = isinstance(exc, PermanentSendError) or a["attempts"] >= (
                        s.delivery_max_attempts
                    )
                    a["status"] = "dead_letter" if dead else "retrying"
                    a["next_attempt_at"] = _now() + timedelta(
                        seconds=s.delivery_backoff_base_s * 2 ** (a["attempts"] - 1)
                    )
                    await db.execute(
                        text(
                            "UPDATE outbound_actions SET status = :st, attempts = :n, "
                            "last_error = :e, next_attempt_at = :t, updated_at = now() "
                            "WHERE id = :id"
                        ),
                        {
                            "st": a["status"],
                            "n": a["attempts"],
                            "e": str(exc),
                            "t": a["next_attempt_at"],
                            "id": a["id"],
                        },
                    )
                    if dead:
                        log.warning("workflow_action_dead_letter", action_id=str(a["id"]))
                        await _audit(
                            db,
                            ctx,
                            "workflow.action.dead_letter",
                            wf_id,
                            {"action_id": a["id"], "error": str(exc)},
                        )
                    continue
                a["status"] = "sent"
                report.reindex += res.reindex
                await db.execute(
                    text(
                        "UPDATE outbound_actions SET status = 'sent', attempts = :n, "
                        "provider_message_id = :p, sent_at = now(), last_error = NULL, "
                        "updated_at = now() WHERE id = :id"
                    ),
                    {"n": a["attempts"], "p": res.provider_message_id, "id": a["id"]},
                )
            if any(a["status"] == "dead_letter" for a in in_stage):
                later = [a for a in acts if a["stage"] > stage and a["status"] != "sent"]
                for a in later:
                    a["status"] = "cancelled"
                if later:
                    await db.execute(
                        text(
                            "UPDATE outbound_actions SET status = 'cancelled', updated_at = now() "
                            "WHERE workflow_id = :wf AND stage > :st AND status <> 'sent'"
                        ),
                        {"wf": wf_id, "st": stage},
                    )
            if any(a["status"] != "sent" for a in in_stage):
                break  # later stages wait until this one is fully sent

        counts = {
            k: sum(1 for a in acts if a["status"] == k)
            for k in ("sent", "retrying", "dead_letter", "cancelled", "queued")
        }
        report.sent, report.retrying = counts["sent"], counts["retrying"] + counts["queued"]
        report.dead_letter, report.cancelled = counts["dead_letter"], counts["cancelled"]
        summary = {**(wf["output"] or {}), "delivery": counts}
        if report.retrying:
            pending = [a["next_attempt_at"] for a in acts if a["status"] in ("queued", "retrying")]
            report.next_in_s = max(0.0, (min(pending) - _now()).total_seconds())
        elif report.dead_letter:
            await _move(
                db,
                wf,
                "failed",
                ctx,
                output=summary,
                error=f"{report.dead_letter} action(s) dead-lettered",
            )
        else:
            await _move(db, wf, "completed", ctx, output=summary, note=f"{report.sent} sent")
        report.status = wf["status"]
        return report

    async def retry_dead_letters(self, db: AsyncSession, ctx: RequestContext, wf_id) -> int:
        """Organizer re-drives a failed workflow after fixing the cause."""
        if ctx.role != "organizer":
            raise NotAllowed("only organizers re-drive dead letters")
        wf = await _lock(db, wf_id)
        if wf is None:
            raise NotFound("workflow not found")
        if wf["status"] != "failed" or not wf["output"] or "delivery" not in wf["output"]:
            raise Conflict("only a workflow that failed during delivery can be retried")
        async with as_system(db, ctx):
            n = (
                await db.execute(
                    text(
                        "UPDATE outbound_actions SET status = 'queued', attempts = 0, "
                        "last_error = NULL, next_attempt_at = now(), updated_at = now(), "
                        "payload = payload - 'simulate' "
                        "WHERE workflow_id = :wf AND status IN ('dead_letter', 'cancelled')"
                    ),
                    {"wf": wf_id},
                )
            ).rowcount
        await _move(db, wf, "executing", ctx, note=f"re-driving {n} action(s)")
        return n

    async def expire(self, db: AsyncSession, ctx: RequestContext, wf_id: uuid.UUID) -> bool:
        async with as_system(db, ctx):
            wf = await _lock(db, wf_id)
        if wf is None or wf["status"] != "awaiting_approval":
            return False
        async with as_system(db, ctx):
            n = (
                await db.execute(
                    text(
                        "UPDATE approvals SET status = 'expired' WHERE workflow_id = :wf "
                        "AND status = 'pending' AND expires_at < now()"
                    ),
                    {"wf": wf_id},
                )
            ).rowcount
        if n:
            await _move(db, wf, "expired", ctx, note="approval window passed")
        return bool(n)
