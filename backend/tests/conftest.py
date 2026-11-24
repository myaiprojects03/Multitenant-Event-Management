"""Shared fixtures. DB tests need the Docker Postgres, migrated and seeded:

docker compose up -d db redis
cd backend; .\\.venv\\Scripts\\alembic upgrade head; .\\.venv\\Scripts\\python -m app.seed
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.context import RequestContext


async def _db_ready() -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            n = (await c.execute(text("SELECT count(*) FROM auth_demo_personas()"))).scalar()
            return bool(n)
    except Exception:
        return False
    finally:
        await engine.dispose()


DB_READY = asyncio.run(_db_ready())
requires_db = pytest.mark.skipif(not DB_READY, reason="Postgres not running/migrated/seeded")


@pytest.fixture
async def rw_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def ro_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().readonly_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


PERSONA_EMAILS = {
    "sara": "sara@northwind-health.example.com",
    "omar": "omar@northwind-health.example.com",
    "ali": "ali@northwind-health.example.com",
    "maya": "maya@medscan.example.com",
    "tom": "tom@cardiovue.example.com",
    "grace": "grace@apex-expo.example.com",
    "leo": "leo@apex-expo.example.com",
    "nina": "nina@cloudforge.example.com",
}


@pytest.fixture
async def ctx_for(rw_engine):
    """ctx_for('maya') -> RequestContext built exactly like the login endpoint does."""

    async def _make(persona: str, event_prefix: str | None = None) -> RequestContext:
        async with rw_engine.connect() as c:
            rows = (
                (
                    await c.execute(
                        text("SELECT * FROM auth_memberships(:e)"), {"e": PERSONA_EMAILS[persona]}
                    )
                )
                .mappings()
                .all()
            )
        if event_prefix:
            rows = [r for r in rows if r["event_name"].startswith(event_prefix)]
        m = rows[0]
        return RequestContext(
            tenant_id=m["tenant_id"],
            event_id=m["event_id"],
            role=m["role"],
            user_id=m["user_id"],
            exhibitor_id=m["exhibitor_id"],
        )

    return _make


def as_uuid(v) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
