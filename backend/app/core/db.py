from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

# Request code connects as app_rw (RLS applies). The owner URL is used only by migrations/seed.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Unscoped app_rw session: sees nothing under RLS. For health checks and login only."""
    async with get_sessionmaker()() as session:
        yield session


_readonly_engine: AsyncEngine | None = None


def get_readonly_engine() -> AsyncEngine:
    """app_readonly: SELECT on approved reporting views only. Used by the BI copilot."""
    global _readonly_engine
    if _readonly_engine is None:
        _readonly_engine = create_async_engine(
            get_settings().readonly_database_url, pool_pre_ping=True, pool_size=3
        )
    return _readonly_engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _readonly_engine
    if _engine is not None:
        await _engine.dispose()
    if _readonly_engine is not None:
        await _readonly_engine.dispose()
    _engine, _sessionmaker, _readonly_engine = None, None, None
