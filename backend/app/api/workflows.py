"""Workflows + HITL approvals.

POST /workflows/trigger            start an agent workflow (exhibitor: lead_follow_up;
                                   organizer: session_summary, schedule_change)
GET  /workflows                    workflows visible to the caller (RLS)
GET  /workflows/{id}               state, history, visible approvals and outbox actions
POST /workflows/{id}/decision      approve / reject (with reason), optional edits to the draft
POST /workflows/{id}/retry         organizer: re-drive dead-lettered actions
GET  /approvals                    the caller's approval queue
GET  /workflows/outbox/sent        simulated inbox: what was actually "sent" (staff)

The agent runs in the worker after the request commits; the response returns at once with
status `pending`, and the console polls GET /workflows/{id}.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jobs
from app.core.context import RequestContext
from app.core.ratelimit import rate_limited
from app.core.redis import get_redis
from app.core.security import CtxDep, DbDep, OrganizerDep
from app.modules.workflows.agents import EditRejected
from app.modules.workflows.engine import WorkflowEngine, WorkflowError
from app.modules.workflows.sender import read_inbox
from app.modules.workflows.states import InvalidTransition

router = APIRouter(tags=["workflows"])


class Simulate(BaseModel):
    fail_times: int = Field(0, ge=0, le=10)
    permanent: bool = False


class LeadFollowUpParams(BaseModel):
    lead_ids: list[uuid.UUID] | None = Field(None, max_length=100)
    tiers: list[Literal["A", "B", "C", "D"]] = ["A", "B"]
    max_messages: int = Field(10, ge=1, le=50)
    simulate: Simulate | None = None


class SessionParams(BaseModel):
    session_id: uuid.UUID
    simulate: Simulate | None = None


class ScheduleChangeParams(SessionParams):
    to_room: str | None = Field(None, max_length=80)


PARAMS = {
    "lead_follow_up": LeadFollowUpParams,
    "session_summary": SessionParams,
    "schedule_change": ScheduleChangeParams,
}


class TriggerRequest(BaseModel):
    kind: Literal["lead_follow_up", "session_summary", "schedule_change"]
    params: dict[str, Any] = {}


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(None, max_length=500)
    edits: dict[str, Any] | None = None


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowError):
        return HTTPException(exc.status_code, str(exc))
    if isinstance(exc, InvalidTransition):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))  # EditRejected


async def _rows(db: AsyncSession, sql: str, params: dict) -> list[dict]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def workflow_detail(db: AsyncSession, wf_id: uuid.UUID) -> dict:
    """Everything the caller may see about one workflow (RLS decides what that is)."""
    wf = await _rows(
        db,
        "SELECT id, kind, status, owner_user_id, owner_exhibitor_id, input, output, error, "
        "history, created_at, updated_at FROM workflows WHERE id = :id",
        {"id": wf_id},
    )
    if not wf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
    approvals = await _rows(
        db,
        "SELECT id, action_type, status, required_role, escalation_level, draft, decided_by, "
        "decided_at, reason, expires_at, created_at FROM approvals WHERE workflow_id = :id "
        "ORDER BY escalation_level",
        {"id": wf_id},
    )
    actions = await _rows(
        db,
        "SELECT id, stage, channel, recipient, subject, status, attempts, last_error, "
        "next_attempt_at, provider_message_id, sent_at FROM outbound_actions "
        "WHERE workflow_id = :id ORDER BY stage, created_at, idempotency_key",
        {"id": wf_id},
    )
    by_status: dict[str, int] = {}
    for a in actions:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    return {**wf[0], "approvals": approvals, "actions": actions, "action_counts": by_status}


def _schedule(background: BackgroundTasks, function: str, ctx: RequestContext, wf_id) -> None:
    # BackgroundTasks run after the response, i.e. after the request transaction committed
    background.add_task(jobs.enqueue, function, str(ctx.tenant_id), str(ctx.event_id), str(wf_id))


@router.post(
    "/workflows/trigger",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limited("llm"))],
)
async def trigger(
    body: TriggerRequest, ctx: CtxDep, db: DbDep, background: BackgroundTasks
) -> dict:
    try:
        params = PARAMS[body.kind].model_validate(body.params).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)
        ) from exc
    try:
        wf_id = await WorkflowEngine(None).trigger(db, ctx, body.kind, params)
    except WorkflowError as exc:
        raise _http(exc) from exc
    _schedule(background, "run_workflow_job", ctx, wf_id)
    return {"workflow_id": wf_id, "status": "pending"}


@router.get("/workflows")
async def list_workflows(
    db: DbDep,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[dict]:
    return await _rows(
        db,
        "SELECT id, kind, status, error, output->>'action_type' AS action_type, "
        "output->'approval_chain' AS approval_chain, created_at, updated_at FROM workflows "
        "WHERE (CAST(:s AS text) IS NULL OR status = :s) ORDER BY created_at DESC LIMIT :n",
        {"s": status_, "n": limit},
    )


@router.get("/approvals")
async def approval_queue(ctx: CtxDep, db: DbDep) -> list[dict]:
    """Pending approvals this caller can decide (organizers don't see exhibitor-level steps)."""
    return await _rows(
        db,
        "SELECT a.id, a.workflow_id, w.kind, a.action_type, a.required_role, "
        "a.escalation_level, a.draft, a.expires_at, a.created_at, w.output->'flags' AS flags, "
        "w.output->'approval_chain' AS approval_chain FROM approvals a "
        "JOIN workflows w ON w.id = a.workflow_id "
        "WHERE a.status = 'pending' AND a.required_role = :role ORDER BY a.created_at",
        {"role": ctx.role},
    )


@router.get("/workflows/outbox/sent")
async def sent_messages(
    ctx: OrganizerDep,
    redis: Annotated[Redis, Depends(get_redis)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    return await read_inbox(redis, ctx.event_id, limit)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: uuid.UUID, db: DbDep) -> dict:
    return await workflow_detail(db, workflow_id)


@router.post("/workflows/{workflow_id}/decision")
async def decide(
    workflow_id: uuid.UUID,
    body: DecisionRequest,
    ctx: CtxDep,
    db: DbDep,
    background: BackgroundTasks,
) -> dict:
    try:
        new_status = await WorkflowEngine(None).decide(
            db, ctx, workflow_id, body.decision, body.reason, body.edits
        )
    except (WorkflowError, EditRejected, InvalidTransition) as exc:
        raise _http(exc) from exc
    if new_status == "executing":
        _schedule(background, "deliver_workflow_job", ctx, workflow_id)
    return await workflow_detail(db, workflow_id)


@router.post("/workflows/{workflow_id}/retry")
async def retry(
    workflow_id: uuid.UUID, ctx: OrganizerDep, db: DbDep, background: BackgroundTasks
) -> dict:
    try:
        n = await WorkflowEngine(None).retry_dead_letters(db, ctx, workflow_id)
    except (WorkflowError, InvalidTransition) as exc:
        raise _http(exc) from exc
    _schedule(background, "deliver_workflow_job", ctx, workflow_id)
    return {"requeued": n, **await workflow_detail(db, workflow_id)}
