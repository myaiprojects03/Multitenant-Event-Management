"""RequestContext: who is calling, for which tenant/event, in which role.

It is built only from a verified JWT (or, for background jobs, explicitly as `system`) and is the
single source of scope for SQL, retrieval, caching and tools. Nothing the LLM says can change it.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker

Role = Literal["attendee", "exhibitor", "organizer", "system"]
USER_ROLES: tuple[Role, ...] = ("attendee", "exhibitor", "organizer")


@dataclass(frozen=True)
class RequestContext:
    tenant_id: uuid.UUID
    event_id: uuid.UUID
    role: Role
    user_id: uuid.UUID | None
    exhibitor_id: uuid.UUID | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if (self.role == "exhibitor") != (self.exhibitor_id is not None):
            raise ValueError("exhibitor_id must be set exactly when role is exhibitor")
        if self.role != "system" and self.user_id is None:
            raise ValueError("user_id is required for user roles")

    @classmethod
    def system(cls, tenant_id: uuid.UUID, event_id: uuid.UUID) -> "RequestContext":
        return cls(tenant_id=tenant_id, event_id=event_id, role="system", user_id=None)

    @property
    def is_staff(self) -> bool:
        return self.role in ("organizer", "system")


_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant, true), set_config('app.event_id', :event, true),"
    " set_config('app.role', :role, true), set_config('app.user_id', :user, true),"
    " set_config('app.exhibitor_id', :exhibitor, true)"
)


async def apply_context(session: AsyncSession, ctx: RequestContext) -> None:
    """Pin the DB transaction to ctx. Settings are transaction-local (is_local=true)."""
    await session.execute(
        _SET_CONTEXT,
        {
            "tenant": str(ctx.tenant_id),
            "event": str(ctx.event_id),
            "role": ctx.role,
            "user": str(ctx.user_id) if ctx.user_id else "",
            "exhibitor": str(ctx.exhibitor_id) if ctx.exhibitor_id else "",
        },
    )


@asynccontextmanager
async def scoped_session(ctx: RequestContext) -> AsyncIterator[AsyncSession]:
    """A session whose single transaction is scoped to ctx; commits on success."""
    async with get_sessionmaker()() as session, session.begin():
        await apply_context(session, ctx)
        yield session
