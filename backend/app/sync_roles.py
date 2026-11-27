"""Set the app roles' passwords from the current settings (run as the owner on every boot).

Migration 0002 sets them once, when it first runs. A host whose secrets can change later
(APP_SECRET rotated, a re-created Space) would otherwise leave the API unable to log in.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def main() -> None:
    s = get_settings()
    # hide_parameters: a failure must not print the password into the logs
    engine = create_async_engine(s.migration_database_url, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            for role, password in (
                ("app_rw", s.app_rw_password),
                ("app_readonly", s.app_readonly_password),
            ):
                # the password travels as a bound parameter (hidden in errors) and is spliced
                # in server-side, so no logged SQL text ever contains it
                await conn.execute(
                    text("SELECT set_config('eventai.role_pw', CAST(:p AS text), true)"),
                    {"p": password.get_secret_value()},
                )
                await conn.execute(
                    text(
                        "DO $$ BEGIN EXECUTE format('ALTER ROLE %I WITH PASSWORD %L', "
                        f"'{role}', current_setting('eventai.role_pw')); END $$"
                    )
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
