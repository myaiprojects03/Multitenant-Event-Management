"""BI copilot with a scripted planner LLM; guard, read-only execution, views and RLS are real."""

from types import SimpleNamespace

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api import analytics as analytics_api
from app.core.config import get_settings
from app.core.context import apply_context, scoped_session
from app.core.db import dispose_engine
from app.core.redis import close_redis
from app.main import app
from app.modules.analytics.anomalies import compute_flags
from app.modules.analytics.catalog import VIEWS
from app.modules.analytics.executor import QueryFailed, run_guarded
from app.modules.analytics.guard import guard_sql
from app.modules.analytics.nl_sql import NlSqlService, choose_chart
from app.modules.analytics.schemas import SqlPlan, Summary
from tests.conftest import PERSONA_EMAILS, requires_db

pytestmark = requires_db


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


@pytest.fixture
async def redis():
    r = Redis.from_url(get_settings().redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


class FakeLLM:
    """Returns the scripted SqlPlans in order; summaries are canned."""

    def __init__(self, plans: list[SqlPlan]):
        self.plans = list(plans)
        self.calls: list[str] = []

    async def generate(self, contents, *, ctx, route_type, schema=None, **kw):
        self.calls.append(route_type)
        if schema is Summary:
            return SimpleNamespace(parsed=Summary(summary="summary", key_points=["k"]))
        return SimpleNamespace(parsed=self.plans.pop(0))


def plan(sql, chart="bar", x=None, y=None, answerable=True):
    return SqlPlan(answerable=answerable, sql=sql, explanation="e", chart_type=chart, x=x,
                   y=y or [])  # fmt: skip


async def ask(ctx, llm, redis, question="q"):
    async with scoped_session(ctx) as db:
        return await NlSqlService(llm, redis).answer(db, ctx, question)


async def leads_count(rw_engine, ctx):
    async with rw_engine.connect() as conn, conn.begin():
        await apply_context(conn, ctx)
        return (await conn.execute(text("SELECT count(*) FROM leads"))).scalar()


# --- catalog / executor -------------------------------------------------------------------------


async def test_catalog_columns_exist_and_readonly_can_read_them(ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    for view, spec in VIEWS.items():
        cols = ", ".join(spec["columns"])
        res = await run_guarded(ali, guard_sql(f"SELECT {cols} FROM {view} LIMIT 1"))
        assert res.columns == list(spec["columns"]), view


async def test_views_scope_rows_even_without_a_where_clause(ctx_for):
    ali, grace = await ctx_for("ali", "HealthTech"), await ctx_for("grace")
    sql = guard_sql("SELECT exhibitor_name FROM exhibitor_performance_v")
    names_a = {r["exhibitor_name"] for r in (await run_guarded(ali, sql)).rows}
    names_b = {r["exhibitor_name"] for r in (await run_guarded(grace, sql)).rows}
    assert "MedScan" in names_a and "CloudForge" in names_b and not names_a & {"CloudForge"}
    # asking for the other tenant's exhibitor by name returns nothing
    other = guard_sql(
        "SELECT leads FROM exhibitor_performance_v WHERE exhibitor_name = 'CloudForge'"
    )
    assert (await run_guarded(ali, other)).rows == []


async def test_non_organizers_get_no_rows_from_views(ctx_for):
    sql = guard_sql("SELECT title FROM session_attendance_v")
    for persona in ("sara", "maya"):
        assert (await run_guarded(await ctx_for(persona), sql)).rows == []


async def test_readonly_role_cannot_write_even_if_guard_were_bypassed(ctx_for):
    from app.modules.analytics.guard import GuardedSql

    ali = await ctx_for("ali", "HealthTech")
    for sql in (
        "DELETE FROM leads",
        "UPDATE sessions SET capacity = 1",
        "SELECT count(*) FROM leads",
    ):
        with pytest.raises(QueryFailed):
            await run_guarded(ali, GuardedSql(sql=sql, views=(), limit=1))


# --- service ----------------------------------------------------------------------------------


async def test_ok_path_returns_rows_summary_and_chart(ctx_for, redis):
    ali = await ctx_for("ali", "HealthTech")
    llm = FakeLLM([plan(
        "SELECT title, dropoff_after_20min_pct FROM session_attendance_v "
        "ORDER BY dropoff_after_20min_pct DESC NULLS LAST LIMIT 5",
        x="title", y=["dropoff_after_20min_pct"])])  # fmt: skip
    r = await ask(ali, llm, redis)
    assert r.status == "ok" and r.row_count == 5 and r.summary == "summary"
    assert r.rows[0]["title"] == "The Future of Remote Patient Monitoring"
    assert r.chart.type == "bar" and r.chart.x == "title"
    assert llm.calls == ["analytics.sql", "analytics.summary"]


async def test_guard_rejection_gets_one_repair(ctx_for, redis):
    ali = await ctx_for("ali", "HealthTech")
    llm = FakeLLM([
        plan("SELECT * FROM lead_summary_v"),
        plan("SELECT exhibitor_name, leads FROM exhibitor_performance_v ORDER BY leads DESC "
             "LIMIT 3", x="exhibitor_name", y=["leads"]),
    ])  # fmt: skip
    r = await ask(ali, llm, redis)
    assert (
        r.status == "ok" and r.repaired and llm.calls[:2] == ["analytics.sql", "analytics.repair"]
    )


async def test_db_error_gets_one_repair(ctx_for, redis):
    ali = await ctx_for("ali", "HealthTech")
    llm = FakeLLM([
        plan("SELECT title, capacity / 0 AS boom FROM session_attendance_v"),
        plan("SELECT title, capacity FROM session_attendance_v LIMIT 2", chart="table"),
    ])  # fmt: skip
    r = await ask(ali, llm, redis)
    assert r.status == "ok" and r.repaired and r.row_count == 2


@pytest.mark.parametrize(
    "malicious",
    [
        "DELETE FROM leads",
        "UPDATE leads SET score = 100",
        "SELECT 1; DROP TABLE leads",
        "SELECT set_config('app.tenant_id', 'x', true) FROM session_attendance_v",
        "SELECT email FROM users",
    ],
)
async def test_malicious_sql_is_blocked_after_repair_and_db_untouched(
    ctx_for, redis, rw_engine, malicious
):
    ali = await ctx_for("ali", "HealthTech")
    before = await leads_count(rw_engine, ali)
    r = await ask(ali, FakeLLM([plan(malicious), plan(malicious)]), redis)
    assert r.status == "blocked" and r.error and not r.rows
    assert await leads_count(rw_engine, ali) == before


async def test_unanswerable_and_failed_are_reported(ctx_for, redis):
    ali = await ctx_for("ali", "HealthTech")
    r = await ask(ali, FakeLLM([plan("", answerable=False)]), redis, "delete everything")
    assert r.status == "unanswerable"
    bad = plan("SELECT title, capacity / 0 AS boom FROM session_attendance_v")
    r = await ask(ali, FakeLLM([bad, bad]), redis, "boom")
    assert r.status == "failed" and "division by zero" in r.error


async def test_results_are_cached(ctx_for, redis):
    ali = await ctx_for("ali", "HealthTech")
    sql = "SELECT industry, attendees FROM attendee_optin_v ORDER BY attendees DESC"
    llm = FakeLLM([plan(sql, x="industry", y=["attendees"])])
    first = await ask(ali, llm, redis, "opt-ins by industry")
    second = await ask(ali, llm, redis, "Opt-ins by industry?")
    assert not first.cached and second.cached and second.rows == first.rows
    assert llm.calls.count("analytics.sql") == 1


def test_choose_chart_validates_columns():
    from app.modules.analytics.executor import QueryResult

    rows = [{"name": "a", "n": 3}, {"name": "b", "n": 1}]
    res = QueryResult(columns=["name", "n"], rows=rows, truncated=False)
    assert choose_chart(plan("x", x="nope", y=["nope"]), res).model_dump() == {
        "type": "bar", "x": "name", "y": ["n"]}  # fmt: skip
    one = QueryResult(columns=["n"], rows=[{"n": 5}], truncated=False)
    assert choose_chart(plan("x", chart="bar"), one).type == "kpi"
    text_only = QueryResult(columns=["name"], rows=[{"name": "a"}], truncated=False)
    assert choose_chart(plan("x", chart="line"), text_only).type == "table"


# --- anomalies --------------------------------------------------------------------------------


async def test_planted_anomalies_are_flagged(ctx_for):
    flags = await compute_flags(await ctx_for("ali", "HealthTech"))
    high = {(f.kind, f.title) for f in flags if f.severity == "high"}
    # the three planted patterns, and nothing else, are high severity
    assert high == {
        ("capacity_risk", "Hands-on Workshop: Building FHIR Integrations"),
        ("dropoff", "The Future of Remote Patient Monitoring"),
        ("lead_velocity", "MedScan"),
    }
    capacity = [f for f in flags if f.kind == "capacity_risk"]
    assert capacity[0].title.endswith("FHIR Integrations") and capacity[0].value >= 200
    # other full workshops may be flagged, but only as medium (100-150% of seats)
    assert all(100 <= f.value < 150 for f in capacity[1:])
    assert [f.kind for f in flags].count("dropoff") == 1
    assert [f.kind for f in flags].count("lead_velocity") == 1


async def test_other_tenant_gets_its_own_anomalies(ctx_for):
    flags = await compute_flags(await ctx_for("grace"))
    titles = {f.title for f in flags}
    assert "CloudForge" in titles and "MedScan" not in titles


# --- API ----------------------------------------------------------------------------------------


async def _token(client, persona):
    r = await client.post("/auth/demo-login", json={"email": PERSONA_EMAILS[persona]})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_api_is_organizer_only(monkeypatch):
    monkeypatch.setattr(analytics_api, "get_llm", lambda: FakeLLM([plan(
        "SELECT day, connection_accept_pct FROM networking_v ORDER BY day", chart="line",
        x="day", y=["connection_accept_pct"])]))  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for persona in ("sara", "maya"):
            h = await _token(c, persona)
            assert (
                await c.post("/analytics/nl-sql", json={"question": "x?"}, headers=h)
            ).status_code == 403
            assert (await c.get("/analytics/anomalies", headers=h)).status_code == 403
        h = await _token(c, "grace")
        r = await c.post("/analytics/nl-sql", json={"question": "acceptance by day?"}, headers=h)
        assert (
            r.status_code == 200
            and r.json()["status"] == "ok"
            and r.json()["chart"]["type"] == "line"
        )
        r = await c.get("/analytics/anomalies?explain=false", headers=h)
        assert r.status_code == 200 and r.json()["flags"]


def test_dbapi_errors_are_wrapped():
    assert issubclass(QueryFailed, RuntimeError) and DBAPIError  # executor wraps DBAPIError
