"""SQL guard: legitimate analytics SQL passes, everything else is rejected (no DB needed)."""

import pytest

from app.modules.analytics.guard import MAX_ROWS, SqlRejected, guard_sql

ALLOWED = [
    "SELECT title, dropoff_after_20min_pct FROM session_attendance_v WHERE checkins >= 20 "
    "ORDER BY dropoff_after_20min_pct DESC NULLS LAST LIMIT 5",
    "SELECT exhibitor_name, sum(qualified_leads) AS q FROM lead_summary_v "
    "WHERE lead_date = current_date - 1 GROUP BY exhibitor_name ORDER BY q DESC",
    "SELECT date_trunc('hour', hour) AS h, kind, sum(events) AS n FROM engagement_v "
    "WHERE day = current_date - 1 GROUP BY 1, 2 ORDER BY 1",
    "WITH d AS (SELECT day, connection_accept_pct FROM networking_v) "
    "SELECT day, round(connection_accept_pct, 1) AS pct FROM d ORDER BY day",
    "SELECT title, to_char(starts_at, 'Dy HH24:MI') AS t, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY checkins) OVER () AS med "
    "FROM session_attendance_v WHERE starts_at > now()",
    "SELECT s.title, l.leads FROM session_attendance_v s CROSS JOIN lead_summary_v l LIMIT 3",
    "SELECT hour, sum(events) FILTER (WHERE kind = 'booth_visit') AS visits FROM engagement_v "
    "GROUP BY hour ORDER BY hour",
    # every boolean operator in one WHERE (regression: AND/OR are Func nodes in sqlglot)
    "SELECT title FROM session_attendance_v WHERE (starts_at > now() AND bookmark_capacity_pct "
    "> 100) OR NOT room IN ('A', 'B') AND checkins BETWEEN 1 AND 5 AND title LIKE '%x%' "
    "AND track IS NOT NULL AND title ILIKE 'a%' OR room <> 'z'",
    "SELECT industry, attendees, rank() OVER (ORDER BY attendees DESC) AS r FROM attendee_optin_v",
    "SELECT exhibitor_name FROM exhibitor_performance_v WHERE leads > "
    "(SELECT avg(leads) FROM exhibitor_performance_v)",
    "SELECT count(*) AS n FROM session_attendance_v",
    "SELECT session_kind, count(*) AS n FROM session_attendance_v GROUP BY session_kind",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_legitimate_sql_passes(sql):
    g = guard_sql(sql)
    assert g.views and "LIMIT" in g.sql.upper()


BLOCKED = {
    "delete": ("DELETE FROM leads", "only SELECT"),
    "update": ("UPDATE session_attendance_v SET capacity = 1", "only SELECT"),
    "insert": ("INSERT INTO leads (score) VALUES (1)", "only SELECT"),
    "drop": ("DROP TABLE leads", "only SELECT"),
    "multi-statement": ("SELECT 1; DROP TABLE leads", "exactly one statement"),
    "two-selects": ("SELECT title FROM session_attendance_v; SELECT 1", "exactly one"),
    "star": ("SELECT * FROM session_attendance_v", "SELECT *"),
    "table-star": ("SELECT s.* FROM session_attendance_v s", "SELECT *"),
    "star-beside-count": ("SELECT count(*), * FROM session_attendance_v", "SELECT *"),
    "base-table": ("SELECT email FROM users", "unknown or forbidden table"),
    "leads-table": ("SELECT score FROM leads", "unknown or forbidden table"),
    "schema-prefix": ("SELECT title FROM public.session_attendance_v", "schema-qualified"),
    "catalog": ("SELECT usename FROM pg_catalog.pg_user", "schema-qualified"),
    "info-schema": ("SELECT table_name FROM information_schema.tables", "schema-qualified"),
    "set-config": (
        "SELECT set_config('app.tenant_id', 'x', true) FROM session_attendance_v",
        "set_config",
    ),
    "current-setting": (
        "SELECT current_setting('app.role') AS r, title FROM session_attendance_v",
        "current_setting",
    ),
    "sleep": ("SELECT pg_sleep(30), title FROM session_attendance_v", "pg_sleep"),
    "file-read": ("SELECT pg_read_file('/etc/passwd'), title FROM session_attendance_v", "pg_read"),
    "dblink": ("SELECT dblink('host=x', 'select 1'), title FROM session_attendance_v", "dblink"),
    "context-fn": ("SELECT app_tenant(), title FROM session_attendance_v", "app_tenant"),
    "version": ("SELECT version(), title FROM session_attendance_v", "not allowed"),
    "tenant-column": ("SELECT tenant_id FROM lead_summary_v", "column not allowed: tenant_id"),
    "event-filter": (
        "SELECT title FROM session_attendance_v WHERE event_id = '00000000-0000-0000-0000-0'",
        "column not allowed: event_id",
    ),
    "select-into": ("SELECT title INTO stolen FROM session_attendance_v", "SELECT INTO"),
    "row-lock": ("SELECT title FROM session_attendance_v FOR UPDATE", "row locks"),
    "dml-in-cte": (
        "WITH x AS (DELETE FROM leads RETURNING 1) SELECT title FROM session_attendance_v",
        "DELETE",
    ),
    "copy": ("COPY (SELECT 1) TO '/tmp/x'", "only SELECT"),
    "set": ("SET app.tenant_id = 'x'", "only SELECT"),
    "dynamic-limit": (
        "SELECT title FROM session_attendance_v LIMIT (SELECT 1000000)",
        "plain integer",
    ),
    "subquery-to-users": (
        "SELECT title FROM session_attendance_v WHERE title = (SELECT email FROM users LIMIT 1)",
        "unknown or forbidden table",
    ),
    "no-view": ("SELECT 1 AS one", "at least one approved view"),
    "empty": ("   ", "empty"),
    "garbage": ("SELEC title FRM x", "parse"),
}


@pytest.mark.parametrize("sql, reason", BLOCKED.values(), ids=BLOCKED.keys())
def test_unsafe_sql_is_blocked(sql, reason):
    with pytest.raises(SqlRejected, match=reason.replace("*", r"\*")):
        guard_sql(sql)


def test_limit_is_injected_and_capped():
    assert guard_sql("SELECT title FROM session_attendance_v").limit == MAX_ROWS
    assert guard_sql("SELECT title FROM session_attendance_v LIMIT 5").limit == 5
    capped = guard_sql("SELECT title FROM session_attendance_v LIMIT 100000")
    assert capped.limit == MAX_ROWS and f"LIMIT {MAX_ROWS}" in capped.sql


def test_very_long_sql_rejected():
    with pytest.raises(SqlRejected, match="too long"):
        guard_sql("SELECT title FROM session_attendance_v WHERE " + " OR ".join(["1=1"] * 2000))
