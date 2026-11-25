"""SQL guard for model-generated analytics queries. Deny by default:

1. parses as PostgreSQL, at most MAX_SQL_CHARS
2. exactly one statement, and it is a SELECT (CTEs / UNION allowed)
3. no SELECT INTO, no row locks (FOR UPDATE)
4. every table is an approved view (or a CTE defined in the query); no schema prefixes
5. every column is an allowed column of those views, or an alias defined in the query; no *
6. every function is on an allowlist (aggregates, math, date, string, window functions);
   anything else (set_config, current_setting, pg_sleep, dblink, file access...) is rejected
7. a LIMIT of at most MAX_ROWS is enforced

The guard is one layer. The query then runs as `app_readonly` (SELECT on these views only,
read-only transactions, 5 s statement timeout), and the views scope rows to the caller's
tenant + event themselves, so even a query that slipped through could not reach other data.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.modules.analytics.catalog import ALLOWED_COLUMNS

MAX_ROWS = 200
MAX_SQL_CHARS = 4000

ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "Count", "Sum", "Avg", "Min", "Max", "Stddev", "StddevSamp", "StddevPop", "Variance",
        "VariancePop", "Corr", "PercentileCont", "PercentileDisc", "ArrayAgg", "GroupConcat",
        "Median", "CountIf",
        # window
        "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile", "Lag", "Lead",
        "FirstValue", "LastValue", "NthValue",
        # math
        "Round", "Abs", "Ceil", "Floor", "Sqrt", "Ln", "Log", "Exp", "Pow", "Mod", "Sign",
        "SafeDivide",
        # conditional / casting
        "Coalesce", "Nullif", "Greatest", "Least", "Case", "If", "Cast", "TryCast",
        # date / time
        "CurrentDate", "CurrentTimestamp", "Extract", "DateTrunc", "TimestampTrunc",
        "TimeToStr", "MakeInterval", "Date", "DateAdd", "DateSub", "DateDiff", "TsOrDsToDate",
        # string
        "Lower", "Upper", "Length", "Concat", "ConcatWs", "Substring", "Trim", "SplitPart",
        "Left", "Right", "Replace", "Initcap",
    }
)  # fmt: skip
# Functions sqlglot doesn't model (parsed as Anonymous), by lower-case name.
ALLOWED_ANONYMOUS = frozenset({"age", "date_part", "to_char", "width_bucket", "stddev"})


class SqlRejected(ValueError):
    """Raised with a short, model-readable reason."""


@dataclass(frozen=True)
class GuardedSql:
    sql: str
    views: tuple[str, ...]
    limit: int


def _reject(reason: str) -> None:
    raise SqlRejected(reason)


def _defined_names(tree: exp.Expression) -> tuple[set[str], set[str]]:
    """CTE names and every alias defined in the query (columns, tables, CTE columns)."""
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for ta in tree.find_all(exp.TableAlias):
        if ta.name:
            aliases.add(ta.name.lower())
        aliases |= {c.name.lower() for c in ta.columns}
    return ctes, aliases


def _check_functions(tree: exp.Expression) -> None:
    for fn in tree.find_all(exp.Func):
        if isinstance(fn, exp.Connector):  # AND / OR / XOR are Func subclasses in sqlglot
            continue
        if isinstance(fn, exp.Anonymous):
            if fn.name.lower() not in ALLOWED_ANONYMOUS:
                _reject(f"function not allowed: {fn.name}")
        elif type(fn).__name__ not in ALLOWED_FUNCTIONS:
            _reject(f"function not allowed: {fn.sql_name().lower()}")


def _enforce_limit(stmt: exp.Expression) -> tuple[exp.Expression, int]:
    limit = stmt.args.get("limit")
    if limit is None:
        return stmt.limit(MAX_ROWS), MAX_ROWS
    value = limit.expression
    if not isinstance(value, exp.Literal) or not value.is_int:
        _reject("LIMIT must be a plain integer")
    n = int(value.this)
    if n > MAX_ROWS:
        return stmt.limit(MAX_ROWS), MAX_ROWS
    return stmt, n


def guard_sql(sql: str) -> GuardedSql:
    if not sql or not sql.strip():
        _reject("empty query")
    if len(sql) > MAX_SQL_CHARS:
        _reject("query too long")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as exc:
        raise SqlRejected(f"could not parse SQL: {str(exc).splitlines()[0][:160]}") from exc
    if len(statements) != 1:
        _reject("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select | exp.Union | exp.Intersect | exp.Except):
        _reject(f"only SELECT is allowed, got {type(stmt).__name__.upper()}")
    for node_type, what in ((exp.Into, "SELECT INTO"), (exp.Lock, "row locks")):
        if stmt.find(node_type):
            _reject(f"{what} not allowed")
    # any data-modifying or DDL node anywhere (e.g. inside a CTE)
    for node_type in (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
                      exp.Alter, exp.Command, exp.Copy, exp.Set):  # fmt: skip
        if stmt.find(node_type):
            _reject(f"{node_type.__name__.upper()} not allowed")

    ctes, aliases = _defined_names(stmt)
    views: set[str] = set()
    for table in stmt.find_all(exp.Table):
        name = table.name.lower()
        if table.args.get("db") or table.args.get("catalog"):
            _reject(f"schema-qualified names are not allowed: {table.sql()}")
        if name in ctes:
            continue
        if name not in ALLOWED_COLUMNS:
            _reject(f"unknown or forbidden table: {name}")
        views.add(name)
    if not views:
        _reject("query must read from at least one approved view")

    if stmt.find(exp.Star):
        _reject("SELECT * is not allowed; list the columns")
    allowed = set().union(*(ALLOWED_COLUMNS[v] for v in views)) | aliases
    for col in stmt.find_all(exp.Column):
        if col.name and col.name.lower() not in allowed:
            _reject(f"column not allowed: {col.name}")

    _check_functions(stmt)
    stmt, limit = _enforce_limit(stmt)
    return GuardedSql(sql=stmt.sql(dialect="postgres"), views=tuple(sorted(views)), limit=limit)
