"""Run guarded SQL as `app_readonly`, scoped to the organizer's tenant + event."""

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import DBAPIError

from app.core.context import RequestContext, apply_context
from app.core.db import get_readonly_engine
from app.modules.analytics.guard import GuardedSql

STATEMENT_TIMEOUT = "5s"


class QueryFailed(RuntimeError):
    """Database rejected or aborted the query (message is safe to show the model)."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    return v


def _db_message(exc: DBAPIError) -> str:
    orig = getattr(exc, "orig", None)
    text = str(orig or exc).splitlines()[0]
    # asyncpg wraps as "<class '...'>: message"
    return text.split(": ", 1)[-1][:300]


async def run_guarded(ctx: RequestContext, guarded: GuardedSql) -> QueryResult:
    async with get_readonly_engine().connect() as conn, conn.begin():
        await apply_context(conn, ctx)
        await conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        try:
            # driver-level execution: SQL text is passed as-is (no ':name' bind parsing)
            result = await conn.exec_driver_sql(guarded.sql)
        except DBAPIError as exc:
            raise QueryFailed(_db_message(exc)) from exc
        columns = list(result.keys())
        rows = [dict(zip(columns, map(_jsonable, r), strict=True)) for r in result.fetchall()]
    return QueryResult(columns=columns, rows=rows, truncated=len(rows) >= guarded.limit)
