"""JWT issue/verify plus FastAPI dependencies for auth and RBAC."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import USER_ROLES, RequestContext, Role, scoped_session

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


def issue_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
    role: Role,
    exhibitor_id: uuid.UUID | None,
) -> tuple[str, int]:
    if role not in USER_ROLES:  # `system` is for background jobs only, never a token
        raise ValueError(f"cannot issue token for role {role!r}")
    s = get_settings()
    ttl = s.jwt_ttl_minutes * 60
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "eid": str(event_id),
        "role": role,
        "xid": str(exhibitor_id) if exhibitor_id else None,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm=ALGORITHM), ttl


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def decode_token(token: str) -> RequestContext:
    try:
        c = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "tid", "eid", "role", "exp"]},
        )
        if c["role"] not in USER_ROLES:
            raise _unauthorized("invalid role")
        return RequestContext(
            tenant_id=uuid.UUID(c["tid"]),
            event_id=uuid.UUID(c["eid"]),
            role=c["role"],
            user_id=uuid.UUID(c["sub"]),
            exhibitor_id=uuid.UUID(c["xid"]) if c.get("xid") else None,
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("token expired") from exc
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise _unauthorized("invalid token") from exc


async def get_ctx(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> RequestContext:
    if creds is None:
        raise _unauthorized("missing bearer token")
    ctx = decode_token(creds.credentials)
    request.state.ctx = ctx
    structlog.contextvars.bind_contextvars(
        request_id=ctx.request_id,
        tenant_id=str(ctx.tenant_id),
        event_id=str(ctx.event_id),
        user_role=ctx.role,
    )
    return ctx


def require_roles(*roles: Role) -> Callable[..., RequestContext]:
    async def _dep(ctx: Annotated[RequestContext, Depends(get_ctx)]) -> RequestContext:
        if ctx.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "role not permitted for this action")
        return ctx

    return _dep


async def get_db(ctx: Annotated[RequestContext, Depends(get_ctx)]) -> AsyncIterator[AsyncSession]:
    """Per-request DB session with tenant/event/role/user pinned for RLS."""
    async with scoped_session(ctx) as session:
        yield session


CtxDep = Annotated[RequestContext, Depends(get_ctx)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
OrganizerDep = Annotated[RequestContext, Depends(require_roles("organizer"))]
