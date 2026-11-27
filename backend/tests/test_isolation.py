"""Phase 1 acceptance: isolation is enforced by the database, even for raw SQL.

Every query here is hand-written SQL executed as `app_rw` (the API's DB role) after setting the
same transaction-local context the API sets from a JWT. Table lists come from the catalog, so a
new table without RLS coverage makes these tests fail.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.context import RequestContext, apply_context
from tests.conftest import requires_db

pytestmark = requires_db


async def q(engine, ctx: RequestContext | None, sql: str, params=None, many: bool = False):
    async with engine.connect() as conn, conn.begin():
        if ctx is not None:
            await apply_context(conn, ctx)
        result = await conn.execute(text(sql), params or {})
        return result.mappings().all() if many else result.scalar()


async def tables_with(engine, column: str) -> list[str]:
    rows = await q(
        engine,
        None,
        """SELECT c.table_name FROM information_schema.columns c
           JOIN information_schema.tables t
             ON t.table_name = c.table_name AND t.table_schema = c.table_schema
           WHERE c.table_schema = 'public' AND c.column_name = :col
             AND t.table_type = 'BASE TABLE' ORDER BY 1""",
        {"col": column},
        many=True,
    )
    return [r["table_name"] for r in rows]


async def expect_rls_violation(engine, ctx, sql, params=None):
    with pytest.raises(DBAPIError) as exc:
        await q(engine, ctx, sql, params)
    assert "row-level security" in str(exc.value) or "permission denied" in str(exc.value)


# --- tenant / event boundary -----------------------------------------------------------------


async def test_catalog_covers_all_tables(rw_engine):
    scoped = await tables_with(rw_engine, "tenant_id")
    assert len(scoped) >= 25, scoped
    no_rls = await q(
        rw_engine,
        None,
        """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'
             AND NOT c.relrowsecurity""",
    )
    assert no_rls == 0
    # RLS binds every role except a table's owner (and superusers): the request roles must be
    # neither, and must not be able to switch it off
    owned_or_privileged = await q(
        rw_engine,
        None,
        """SELECT count(*) FROM pg_roles r
           WHERE r.rolname IN ('app_rw', 'app_readonly')
             AND (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR EXISTS (
                  SELECT 1 FROM pg_class c WHERE c.relowner = r.oid))""",
    )
    assert owned_or_privileged == 0
    assert await q(rw_engine, None, "SELECT current_user") == "app_rw"


async def test_no_context_sees_nothing(rw_engine):
    for table in ["tenants", *await tables_with(rw_engine, "tenant_id")]:
        assert await q(rw_engine, None, f"SELECT count(*) FROM {table}") == 0, table


@pytest.mark.parametrize("persona", ["sara", "maya", "ali", "grace", "leo", "nina"])
async def test_zero_rows_from_other_tenants_or_events(rw_engine, ctx_for, persona):
    ctx = await ctx_for(persona)
    other_tenant = await ctx_for(
        "grace" if ctx.tenant_id != (await ctx_for("grace")).tenant_id else "ali"
    )
    for table in await tables_with(rw_engine, "tenant_id"):
        leaked = await q(
            rw_engine, ctx, f"SELECT count(*) FROM {table} WHERE tenant_id <> app_tenant()"
        )
        assert leaked == 0, f"{persona} sees other-tenant rows in {table}"
        explicit = await q(
            rw_engine,
            ctx,
            f"SELECT count(*) FROM {table} WHERE tenant_id = :t",
            {"t": other_tenant.tenant_id},
        )
        assert explicit == 0, f"{persona} can select tenant {other_tenant.tenant_id} in {table}"
    for table in await tables_with(rw_engine, "event_id"):
        leaked = await q(
            rw_engine, ctx, f"SELECT count(*) FROM {table} WHERE event_id <> app_event()"
        )
        assert leaked == 0, f"{persona} sees other-event rows in {table}"
    assert await q(rw_engine, ctx, "SELECT count(*) FROM tenants WHERE id <> app_tenant()") == 0


async def test_same_organizer_is_scoped_per_event(rw_engine, ctx_for):
    summit = await ctx_for("ali", "HealthTech")
    forum = await ctx_for("ali", "Clinical AI Forum")
    assert summit.tenant_id == forum.tenant_id and summit.event_id != forum.event_id
    n_summit = await q(rw_engine, summit, "SELECT count(*) FROM sessions")
    n_forum = await q(rw_engine, forum, "SELECT count(*) FROM sessions")
    assert (n_summit, n_forum) == (40, 8)


async def test_organizer_sees_data_so_checks_are_not_vacuous(rw_engine, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    for table in ("sessions", "leads", "attendee_profiles", "engagement_logs", "users"):
        assert await q(rw_engine, ali, f"SELECT count(*) FROM {table}") > 0, table


# --- exhibitors vs competitors -----------------------------------------------------------------


async def test_exhibitor_cannot_read_competitor_leads(rw_engine, ctx_for):
    maya, tom = await ctx_for("maya"), await ctx_for("tom")
    own = await q(rw_engine, maya, "SELECT count(*) FROM leads")
    assert own > 0
    assert (
        await q(
            rw_engine,
            maya,
            "SELECT count(*) FROM leads WHERE exhibitor_id <> :x",
            {"x": maya.exhibitor_id},
        )
        == 0
    )
    assert (
        await q(
            rw_engine,
            maya,
            "SELECT count(*) FROM leads WHERE exhibitor_id = :x",
            {"x": tom.exhibitor_id},
        )
        == 0
    )
    # the organizer can confirm the competitor really has leads
    ali = await ctx_for("ali", "HealthTech")
    assert (
        await q(
            rw_engine,
            ali,
            "SELECT count(*) FROM leads WHERE exhibitor_id = :x",
            {"x": tom.exhibitor_id},
        )
        > 0
    )


async def test_exhibitor_private_material_stays_with_owner(rw_engine, ctx_for):
    maya, tom = await ctx_for("maya"), await ctx_for("tom")
    for table in ("documents", "faqs", "embeddings"):
        sql = f"SELECT count(*) FROM {table} WHERE owner_exhibitor_id = :x"
        assert await q(rw_engine, maya, sql, {"x": tom.exhibitor_id}) == 0, table
    assert (
        await q(
            rw_engine,
            maya,
            "SELECT count(*) FROM documents WHERE owner_exhibitor_id = :x",
            {"x": maya.exhibitor_id},
        )
        == 1
    )
    assert await q(rw_engine, maya, "SELECT count(*) FROM exhibitor_lead_criteria") == 1
    assert await q(rw_engine, maya, "SELECT count(*) FROM exhibitor_prospects_v WHERE is_lead") > 0


async def test_exhibitor_sees_contact_details_only_for_own_leads(rw_engine, ctx_for):
    maya, tom, sara = await ctx_for("maya"), await ctx_for("tom"), await ctx_for("sara")
    # Sara accepted a MedScan meeting (lead) but only has a pending request from CardioVue
    assert (
        await q(rw_engine, maya, "SELECT count(*) FROM users WHERE id = :u", {"u": sara.user_id})
        == 1
    )
    assert (
        await q(rw_engine, tom, "SELECT count(*) FROM users WHERE id = :u", {"u": sara.user_id})
        == 0
    )
    visible = await q(rw_engine, maya, "SELECT count(*) FROM users WHERE id <> app_user()")
    leads = await q(rw_engine, maya, "SELECT count(DISTINCT attendee_user_id) FROM leads")
    assert visible == leads


async def test_prospects_view_is_anonymized_and_consented(rw_engine, ctx_for):
    maya, ali = await ctx_for("maya"), await ctx_for("ali", "HealthTech")
    rows = await q(rw_engine, maya, "SELECT * FROM exhibitor_prospects_v LIMIT 5", many=True)
    assert rows
    assert not {"display_name", "company", "user_id", "email", "full_name"} & set(rows[0].keys())
    n_view = await q(rw_engine, maya, "SELECT count(*) FROM exhibitor_prospects_v")
    n_consented = await q(
        rw_engine, ali, "SELECT count(*) FROM attendee_profiles WHERE exhibitor_matching_opt_in"
    )
    assert n_view == n_consented
    # non-exhibitors get nothing from it
    assert (
        await q(rw_engine, await ctx_for("sara"), "SELECT count(*) FROM exhibitor_prospects_v") == 0
    )


# --- attendees --------------------------------------------------------------------------------


async def test_attendee_sees_only_opted_in_peers(rw_engine, ctx_for):
    sara, omar = await ctx_for("sara"), await ctx_for("omar")
    hidden = await q(
        rw_engine,
        sara,
        "SELECT count(*) FROM attendee_profiles"
        " WHERE NOT networking_opt_in AND user_id <> app_user()",
    )
    assert hidden == 0
    assert (
        await q(
            rw_engine,
            sara,
            "SELECT count(*) FROM attendee_profiles WHERE user_id = :u",
            {"u": omar.user_id},
        )
        == 0
    )
    assert await q(rw_engine, sara, "SELECT count(*) FROM attendee_profiles") > 100


async def test_attendee_cannot_read_leads_or_logs_of_others(rw_engine, ctx_for):
    sara = await ctx_for("sara")
    assert (
        await q(rw_engine, sara, "SELECT count(*) FROM leads WHERE attendee_user_id <> app_user()")
        == 0
    )
    assert await q(rw_engine, sara, "SELECT count(*) FROM leads") >= 1  # her own MedScan lead
    for table in ("engagement_logs", "bookmarks"):
        assert (
            await q(rw_engine, sara, f"SELECT count(*) FROM {table} WHERE user_id <> app_user()")
            == 0
        ), table
    for table in (
        "audit_logs",
        "llm_usage_logs",
        "workflows",
        "approvals",
        "exhibitor_lead_criteria",
    ):
        assert await q(rw_engine, sara, f"SELECT count(*) FROM {table}") == 0, table


async def test_visibility_matrix(rw_engine, ctx_for):
    sara, maya = await ctx_for("sara"), await ctx_for("maya")
    ali = await ctx_for("ali", "HealthTech")

    async def vis(ctx):
        rows = await q(rw_engine, ctx, "SELECT DISTINCT visibility FROM faqs", many=True)
        return {r["visibility"] for r in rows}

    assert await vis(sara) == {"public", "attendee"}
    assert await vis(maya) == {"public", "exhibitor_private"}
    assert await vis(ali) == {"public", "attendee", "exhibitor_private", "organizer_only"}


# --- writes -------------------------------------------------------------------------------------


async def test_writes_cannot_cross_tenant_user_or_exhibitor(rw_engine, ctx_for):
    sara, maya, tom = await ctx_for("sara"), await ctx_for("maya"), await ctx_for("tom")
    grace = await ctx_for("grace")
    session_id = await q(rw_engine, sara, "SELECT id FROM sessions LIMIT 1")
    # bookmark into another tenant
    await expect_rls_violation(
        rw_engine,
        sara,
        "INSERT INTO bookmarks (tenant_id, event_id, user_id, session_id) VALUES (:t, :e, :u, :s)",
        {"t": grace.tenant_id, "e": grace.event_id, "u": sara.user_id, "s": session_id},
    )
    # bookmark on behalf of someone else
    await expect_rls_violation(
        rw_engine,
        sara,
        "INSERT INTO bookmarks (tenant_id, event_id, user_id, session_id) VALUES (:t, :e, :u, :s)",
        {"t": sara.tenant_id, "e": sara.event_id, "u": tom.user_id, "s": session_id},
    )
    # exhibitor creating a lead for a competitor
    await expect_rls_violation(
        rw_engine,
        maya,
        """INSERT INTO leads
             (tenant_id, event_id, exhibitor_id, attendee_user_id, source, captured_at)
           VALUES (:t, :e, :x, :u, 'badge_scan', now())""",
        {"t": maya.tenant_id, "e": maya.event_id, "x": tom.exhibitor_id, "u": sara.user_id},
    )
    # updates/deletes on invisible rows silently affect nothing
    async with rw_engine.connect() as conn, conn.begin():
        await apply_context(conn, maya)
        res = await conn.execute(
            text("DELETE FROM leads WHERE exhibitor_id = :x"), {"x": tom.exhibitor_id}
        )
        assert res.rowcount == 0
        await conn.rollback()


async def test_attendee_can_write_own_bookmark(rw_engine, ctx_for):
    sara = await ctx_for("sara")
    async with rw_engine.connect() as conn, conn.begin():
        await apply_context(conn, sara)
        sid = (
            await conn.execute(
                text(
                    "SELECT id FROM sessions"
                    " WHERE id NOT IN (SELECT session_id FROM bookmarks) LIMIT 1"
                )
            )
        ).scalar()
        await conn.execute(
            text(
                "INSERT INTO bookmarks (tenant_id, event_id, user_id, session_id) "
                "VALUES (app_tenant(), app_event(), app_user(), :s)"
            ),
            {"s": sid},
        )
        await conn.rollback()


# --- reporting views + read-only role -------------------------------------------------------------


async def test_reporting_views_are_organizer_only_and_scoped(rw_engine, ro_engine, ctx_for):
    views = (
        "session_attendance_v",
        "lead_summary_v",
        "exhibitor_performance_v",
        "engagement_v",
        "networking_v",
        "attendee_optin_v",
    )
    sara, maya = await ctx_for("sara"), await ctx_for("maya")
    ali, grace = await ctx_for("ali", "HealthTech"), await ctx_for("grace")
    for v in views:
        assert await q(ro_engine, None, f"SELECT count(*) FROM {v}") == 0, v
        assert await q(ro_engine, sara, f"SELECT count(*) FROM {v}") == 0, v
        assert await q(ro_engine, maya, f"SELECT count(*) FROM {v}") == 0, v
        assert await q(ro_engine, ali, f"SELECT count(*) FROM {v}") > 0, v
        assert (
            await q(
                ro_engine,
                ali,
                f"SELECT count(*) FROM {v} WHERE tenant_id = :t",
                {"t": grace.tenant_id},
            )
            == 0
        ), v


@pytest.mark.parametrize("table", ["leads", "users", "attendee_profiles", "sessions"])
async def test_readonly_role_cannot_touch_base_tables(ro_engine, ctx_for, table):
    ali = await ctx_for("ali", "HealthTech")
    with pytest.raises(DBAPIError, match="permission denied"):
        await q(ro_engine, ali, f"SELECT count(*) FROM {table}")


async def test_readonly_role_cannot_write(ro_engine, ctx_for):
    ali = await ctx_for("ali", "HealthTech")
    with pytest.raises(DBAPIError):
        await q(ro_engine, ali, "DELETE FROM session_attendance_v")
    with pytest.raises(DBAPIError, match="read-only"):
        await q(ro_engine, ali, "CREATE TEMP TABLE x (id int)")


@requires_db
async def test_rls_bypass_guard(rw_engine):
    """Serving or indexing over a connection that skips RLS (owner/superuser) is refused."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings
    from app.core.db import RlsBypassError, assert_rls_applies

    await assert_rls_applies(rw_engine)  # app_rw: fine
    owner = create_async_engine(get_settings().migration_database_url)
    try:
        with pytest.raises(RlsBypassError):
            await assert_rls_applies(owner)
    finally:
        await owner.dispose()
