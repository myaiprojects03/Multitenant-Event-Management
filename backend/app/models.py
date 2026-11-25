"""ORM models. Every event-scoped table carries tenant_id + event_id; RLS lives in migrations.

Exceptions (by nature): `tenants` is the root, `events` is scoped by tenant_id with id = event_id,
`users` is tenant-scoped (a person can hold roles in several events of one tenant).
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

EMBEDDING_DIM = 384

ROLES = ("attendee", "exhibitor", "organizer")
VISIBILITIES = ("public", "attendee", "exhibitor_private", "organizer_only")

uuid_pk = Annotated[
    uuid.UUID,
    mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
]
created_ts = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
]
str_list = Annotated[
    list[str], mapped_column(ARRAY(Text), server_default=text("'{}'"), nullable=False)
]


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class TenantScoped:
    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        )


class EventScoped(TenantScoped):
    @declared_attr
    def event_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        )

    @declared_attr.directive
    def __table_args__(cls):
        extra = getattr(cls, "__extra_args__", ())
        kwargs = getattr(cls, "__table_kwargs__", {})
        return (Index(f"ix_{cls.__tablename__}_scope", "tenant_id", "event_id"), *extra, kwargs)


# Append-only logs are written by roles that may not read them back (RLS). Postgres checks
# RETURNING rows against the read policy, so these tables must not use implicit RETURNING.
_APPEND_ONLY = {"implicit_returning": False}


def _fk(target: str, nullable: bool = False, ondelete: str = "CASCADE"):
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


# --- core -------------------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[created_ts]


class Event(TenantScoped, Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_tenant", "tenant_id"),)
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # healthcare | tech | ...
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    # bumped on content changes; part of cache keys so stale answers are never served
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[created_ts]


class User(TenantScoped, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email"), Index("ix_users_tenant", "tenant_id"))
    id: Mapped[uuid_pk]
    email: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    is_demo_persona: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    persona_label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_ts]


class UserEventRole(EventScoped, Base):
    __tablename__ = "user_event_roles"
    __extra_args__ = (
        UniqueConstraint("event_id", "user_id"),
        CheckConstraint(_in("role", ROLES), name="role_valid"),
        CheckConstraint(
            "(role = 'exhibitor') = (exhibitor_id IS NOT NULL)", name="exhibitor_role_has_exhibitor"
        ),
    )
    id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    role: Mapped[str] = mapped_column(Text, nullable=False)
    exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)


class AttendeeProfile(EventScoped, Base):
    __tablename__ = "attendee_profiles"
    __extra_args__ = (UniqueConstraint("event_id", "user_id"),)
    id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    seniority: Mapped[str | None] = mapped_column(Text)
    persona_type: Mapped[str | None] = mapped_column(Text)  # buyer | seller | mentor | mentee | ...
    goals: Mapped[str | None] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)
    interests: Mapped[str_list]
    tags: Mapped[str_list]
    looking_for: Mapped[str_list]
    offering: Mapped[str_list]
    # two separate opt-ins (locked decision)
    networking_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    exhibitor_matching_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    updated_at: Mapped[created_ts]


# --- program ----------------------------------------------------------------------------------


class Track(EventScoped, Base):
    __tablename__ = "tracks"
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Room(EventScoped, Base):
    __tablename__ = "rooms"
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    floor: Mapped[str | None] = mapped_column(Text)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)


class Speaker(EventScoped, Base):
    __tablename__ = "speakers"
    id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[str_list]


class Session(EventScoped, Base):
    __tablename__ = "sessions"
    __extra_args__ = (
        CheckConstraint("ends_at > starts_at", name="session_time_order"),
        CheckConstraint(_in("kind", ("keynote", "talk", "panel", "workshop")), name="kind_valid"),
    )
    id: Mapped[uuid_pk]
    track_id: Mapped[uuid.UUID | None] = _fk("tracks.id", nullable=True, ondelete="SET NULL")
    room_id: Mapped[uuid.UUID | None] = _fk("rooms.id", nullable=True, ondelete="SET NULL")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    tags: Mapped[str_list]


class SessionSpeaker(EventScoped, Base):
    __tablename__ = "session_speakers"
    __extra_args__ = (UniqueConstraint("session_id", "speaker_id"),)
    id: Mapped[uuid_pk]
    session_id: Mapped[uuid.UUID] = _fk("sessions.id")
    speaker_id: Mapped[uuid.UUID] = _fk("speakers.id")


class Exhibitor(EventScoped, Base):
    """Public exhibitor directory info only. Private criteria live in exhibitor_lead_criteria."""

    __tablename__ = "exhibitors"
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    booth: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str_list]
    website: Mapped[str | None] = mapped_column(Text)


class ExhibitorLeadCriteria(EventScoped, Base):
    __tablename__ = "exhibitor_lead_criteria"
    __extra_args__ = (UniqueConstraint("exhibitor_id"),)
    id: Mapped[uuid_pk]
    exhibitor_id: Mapped[uuid.UUID] = _fk("exhibitors.id")
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Sponsor(EventScoped, Base):
    __tablename__ = "sponsors"
    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    exhibitor_id: Mapped[uuid.UUID | None] = _fk(
        "exhibitors.id", nullable=True, ondelete="SET NULL"
    )
    website: Mapped[str | None] = mapped_column(Text)


_visibility_check = CheckConstraint(_in("visibility", VISIBILITIES), name="visibility_valid")
_owner_check = CheckConstraint(
    "(visibility = 'exhibitor_private') = (owner_exhibitor_id IS NOT NULL)",
    name="private_has_owner",
)


class Faq(EventScoped, Base):
    __tablename__ = "faqs"
    __extra_args__ = (_visibility_check, _owner_check)
    id: Mapped[uuid_pk]
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    owner_exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)


class Document(EventScoped, Base):
    __tablename__ = "documents"
    __extra_args__ = (_visibility_check, _owner_check)
    id: Mapped[uuid_pk]
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # pdf | text
    content: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    owner_exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)
    created_at: Mapped[created_ts]


# --- interactions -----------------------------------------------------------------------------


class Bookmark(EventScoped, Base):
    __tablename__ = "bookmarks"
    __extra_args__ = (UniqueConstraint("user_id", "session_id"),)
    id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    session_id: Mapped[uuid.UUID] = _fk("sessions.id")
    created_at: Mapped[created_ts]


class Connection(EventScoped, Base):
    __tablename__ = "connections"
    __extra_args__ = (
        UniqueConstraint("event_id", "requester_id", "addressee_id"),
        CheckConstraint("requester_id <> addressee_id", name="no_self_connection"),
        CheckConstraint(_in("status", ("pending", "accepted", "declined")), name="status_valid"),
    )
    id: Mapped[uuid_pk]
    requester_id: Mapped[uuid.UUID] = _fk("users.id")
    addressee_id: Mapped[uuid.UUID] = _fk("users.id")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_ts]
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeetingRequest(EventScoped, Base):
    __tablename__ = "meeting_requests"
    __extra_args__ = (
        CheckConstraint(_in("initiated_by", ("attendee", "exhibitor")), name="initiator_valid"),
        CheckConstraint(
            _in("status", ("pending", "accepted", "declined", "cancelled")), name="status_valid"
        ),
    )
    id: Mapped[uuid_pk]
    attendee_user_id: Mapped[uuid.UUID] = _fk("users.id")
    exhibitor_id: Mapped[uuid.UUID] = _fk("exhibitors.id")
    initiated_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_ts]
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Lead(EventScoped, Base):
    """Exists only after the attendee accepted a meeting or had their badge scanned."""

    __tablename__ = "leads"
    __extra_args__ = (
        UniqueConstraint("exhibitor_id", "attendee_user_id"),
        CheckConstraint(_in("source", ("badge_scan", "meeting_accepted")), name="source_valid"),
        Index("ix_leads_exhibitor_captured", "exhibitor_id", "captured_at"),
    )
    id: Mapped[uuid_pk]
    exhibitor_id: Mapped[uuid.UUID] = _fk("exhibitors.id")
    attendee_user_id: Mapped[uuid.UUID] = _fk("users.id")
    source: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    tier: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


ENGAGEMENT_KINDS = ("session_check_in", "session_check_out", "booth_visit", "click", "app_open")


class EngagementLog(EventScoped, Base):
    __tablename__ = "engagement_logs"
    __table_kwargs__ = _APPEND_ONLY
    __extra_args__ = (
        CheckConstraint(_in("kind", ENGAGEMENT_KINDS), name="kind_valid"),
        Index("ix_engagement_session", "session_id", "kind"),
        Index("ix_engagement_occurred", "event_id", "occurred_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[uuid.UUID | None] = _fk("sessions.id", nullable=True)
    exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


# --- AI / retrieval ---------------------------------------------------------------------------


class Embedding(EventScoped, Base):
    __tablename__ = "embeddings"
    __extra_args__ = (
        _visibility_check,
        _owner_check,
        UniqueConstraint("event_id", "entity_type", "entity_id", "chunk_index"),
        Index("ix_embeddings_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[uuid_pk]
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    owner_exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(title, '') || ' ' || content)")
    )
    updated_at: Mapped[created_ts]


# --- workflows / HITL -------------------------------------------------------------------------

WORKFLOW_STATUSES = (
    "pending", "running", "awaiting_approval", "approved", "rejected", "expired",
    "executing", "completed", "failed",
)  # fmt: skip


class Workflow(EventScoped, Base):
    __tablename__ = "workflows"
    __extra_args__ = (CheckConstraint(_in("status", WORKFLOW_STATUSES), name="status_valid"),)
    id: Mapped[uuid_pk]
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    owner_user_id: Mapped[uuid.UUID] = _fk("users.id")
    owner_exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_ts]
    updated_at: Mapped[created_ts]


class Approval(EventScoped, Base):
    __tablename__ = "approvals"
    __extra_args__ = (
        CheckConstraint(
            _in("status", ("pending", "approved", "rejected", "expired")), name="status_valid"
        ),
        CheckConstraint(_in("required_role", ROLES), name="required_role_valid"),
    )
    id: Mapped[uuid_pk]
    workflow_id: Mapped[uuid.UUID] = _fk("workflows.id")
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    required_role: Mapped[str] = mapped_column(Text, nullable=False)
    approver_exhibitor_id: Mapped[uuid.UUID | None] = _fk("exhibitors.id", nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decided_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[created_ts]


class ApprovalPolicy(EventScoped, Base):
    __tablename__ = "approval_policies"
    __extra_args__ = (
        UniqueConstraint("event_id", "action_type"),
        CheckConstraint(_in("approver_role", ROLES), name="approver_role_valid"),
    )
    id: Mapped[uuid_pk]
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    approver_role: Mapped[str] = mapped_column(Text, nullable=False)
    bulk_threshold: Mapped[int | None] = mapped_column(Integer)
    escalate_to_role: Mapped[str | None] = mapped_column(Text)
    expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="48")


# --- observability ----------------------------------------------------------------------------


class AuditLog(EventScoped, Base):
    __tablename__ = "audit_logs"
    __table_kwargs__ = _APPEND_ONLY
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    actor_role: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[created_ts]


class LlmUsageLog(EventScoped, Base):
    __tablename__ = "llm_usage_logs"
    __table_kwargs__ = _APPEND_ONLY
    __extra_args__ = (Index("ix_llm_usage_created", "event_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    user_role: Mapped[str | None] = mapped_column(Text)
    route_type: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_ts]
