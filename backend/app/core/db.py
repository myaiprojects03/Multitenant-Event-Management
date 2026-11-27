from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.loops import current_loop

# Request code connects as app_rw (RLS applies). The owner URL is used only by migrations/seed.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_engine_loop = None


def _pool_args() -> dict:
    # serverless: many small instances share the database's connection limit
    return {"pool_size": 2, "max_overflow": 3} if get_settings().serverless else {}


def _stale(loop) -> bool:
    """Serverless: a pool built on a finished event loop can't be reused."""
    return get_settings().serverless and loop is not current_loop()


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker, _engine_loop
    if _engine is None or _stale(_engine_loop):
        _engine = create_async_engine(
            get_settings().database_url, pool_pre_ping=True, **_pool_args()
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_loop = current_loop()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()  # (re)builds the engine and its sessionmaker together
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Unscoped app_rw session: sees nothing under RLS. For health checks and login only."""
    async with get_sessionmaker()() as session:
        yield session


_readonly_engine: AsyncEngine | None = None
_readonly_loop = None


def get_readonly_engine() -> AsyncEngine:
    """app_readonly: SELECT on approved reporting views only. Used by the BI copilot."""
    global _readonly_engine, _readonly_loop
    if _readonly_engine is None or _stale(_readonly_loop):
        _readonly_engine = create_async_engine(
            get_settings().readonly_database_url,
            pool_pre_ping=True,
            **(_pool_args() or {"pool_size": 3}),
        )
        _readonly_loop = current_loop()
    return _readonly_engine


class RlsBypassError(RuntimeError):
    """The app connection would skip row-level security: refuse to serve or index with it."""


_BYPASSES_RLS = """
SELECT r.rolsuper OR r.rolbypassrls OR EXISTS (
         SELECT 1 FROM pg_class c WHERE c.relowner = r.oid AND c.relrowsecurity)
FROM pg_roles r WHERE r.rolname = current_user
"""


async def assert_rls_applies(engine: AsyncEngine | None = None) -> None:
    """Tenant isolation rests on RLS binding the app role. A superuser, a BYPASSRLS role or a
    table owner (e.g. DATABASE_URL accidentally set to the owner) would see every tenant."""
    engine = engine or get_engine()
    async with engine.connect() as conn:
        user = (await conn.execute(text("SELECT current_user"))).scalar()
        if (await conn.execute(text(_BYPASSES_RLS))).scalar():
            raise RlsBypassError(
                f"DATABASE_URL connects as {user!r}, which bypasses row-level security; "
                "it must be the app_rw role"
            )


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _readonly_engine
    if _engine is not None:
        await _engine.dispose()
    if _readonly_engine is not None:
        await _readonly_engine.dispose()
    _engine, _sessionmaker, _readonly_engine = None, None, None
