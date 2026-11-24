import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    # reporting views and SQL-defined objects are managed by hand-written migrations
    return not (type_ == "table" and reflected and compare_to is None)


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        compare_type=True,
    )


def _run_sync(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    # migrations always run as the owner role, never as app_rw
    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
asyncio.run(_run_online())
