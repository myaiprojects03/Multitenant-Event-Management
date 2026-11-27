"""Security layer: request-context functions, DB roles, row-level security, auth lookups.

How isolation works
-------------------
Each request transaction runs as `app_rw` (not the table owner, so RLS always applies) and first
calls set_config('app.tenant_id' | 'app.event_id' | 'app.role' | 'app.user_id' |
'app.exhibitor_id', ..., is_local => true) from the verified JWT. Every policy below reads those
settings. If they are missing, the helper functions return NULL and every policy evaluates to
NULL, so the default is zero rows (fail closed).

Roles inside the app: attendee | exhibitor | organizer come from login; `system` is used only by
background jobs (ingestion, agents) and is never issued in a JWT.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import get_settings
from migrations.sqlutil import exec_script

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTEXT_FUNCTIONS = """
CREATE OR REPLACE FUNCTION app_tenant() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;
CREATE OR REPLACE FUNCTION app_event() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.event_id', true), '')::uuid $$;
CREATE OR REPLACE FUNCTION app_user() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
CREATE OR REPLACE FUNCTION app_exhibitor() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.exhibitor_id', true), '')::uuid $$;
CREATE OR REPLACE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.role', true), '') $$;

-- organizer or background system job
CREATE OR REPLACE FUNCTION app_is_staff() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(app_role() IN ('organizer', 'system'), false) $$;

CREATE OR REPLACE FUNCTION app_is_exhibitor(p_exhibitor uuid) RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(app_role() = 'exhibitor' AND p_exhibitor = app_exhibitor(), false) $$;

-- visibility matrix: public | attendee | exhibitor_private (+owner) | organizer_only
CREATE OR REPLACE FUNCTION app_can_see(p_visibility text, p_owner uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT CASE p_visibility
    WHEN 'public'            THEN app_role() IN ('attendee', 'exhibitor', 'organizer', 'system')
    WHEN 'attendee'          THEN app_role() IN ('attendee', 'organizer', 'system')
    WHEN 'exhibitor_private' THEN app_is_staff() OR app_is_exhibitor(p_owner)
    WHEN 'organizer_only'    THEN app_is_staff()
    ELSE false
  END
$$;
"""

SCOPE = "tenant_id = app_tenant() AND event_id = app_event()"
STAFF = "app_is_staff()"

# table -> (read predicate, write predicate). Both are ANDed with SCOPE unless noted.
# write=None: nobody may write through app_rw. write="INSERT_ONLY": append-only log.
EVENT_TABLES: dict[str, tuple[str, str | None]] = {
    "user_event_roles": (f"user_id = app_user() OR {STAFF}", STAFF),
    "attendee_profiles": (
        f"""user_id = app_user() OR {STAFF}
            OR (app_role() = 'attendee' AND networking_opt_in)
            OR (app_role() = 'exhibitor' AND EXISTS (
                SELECT 1 FROM leads l WHERE l.attendee_user_id = attendee_profiles.user_id
                  AND l.event_id = attendee_profiles.event_id
                  AND l.exhibitor_id = app_exhibitor()))""",
        f"user_id = app_user() OR {STAFF}",
    ),
    "tracks": ("true", STAFF),
    "rooms": ("true", STAFF),
    "speakers": ("true", STAFF),
    "sessions": ("true", STAFF),
    "session_speakers": ("true", STAFF),
    "exhibitors": ("true", f"{STAFF} OR app_is_exhibitor(id)"),
    "sponsors": ("true", STAFF),
    "exhibitor_lead_criteria": (
        f"{STAFF} OR app_is_exhibitor(exhibitor_id)",
        f"{STAFF} OR app_is_exhibitor(exhibitor_id)",
    ),
    "faqs": ("app_can_see(visibility, owner_exhibitor_id)", STAFF),
    "documents": (
        "app_can_see(visibility, owner_exhibitor_id)",
        f"{STAFF} OR (visibility = 'exhibitor_private' AND app_is_exhibitor(owner_exhibitor_id))",
    ),
    "embeddings": ("app_can_see(visibility, owner_exhibitor_id)", STAFF),
    "bookmarks": (f"user_id = app_user() OR {STAFF}", f"user_id = app_user() OR {STAFF}"),
    "connections": (
        f"app_user() IN (requester_id, addressee_id) OR {STAFF}",
        f"app_user() IN (requester_id, addressee_id) OR {STAFF}",
    ),
    "meeting_requests": (
        f"attendee_user_id = app_user() OR app_is_exhibitor(exhibitor_id) OR {STAFF}",
        f"attendee_user_id = app_user() OR app_is_exhibitor(exhibitor_id) OR {STAFF}",
    ),
    # competitors never see each other's leads; an attendee sees the leads that are about them
    "leads": (
        f"app_is_exhibitor(exhibitor_id) OR attendee_user_id = app_user() OR {STAFF}",
        f"app_is_exhibitor(exhibitor_id) OR {STAFF}",
    ),
    # exhibitors get booth traffic only as aggregates, never per-visitor rows
    "engagement_logs": (f"user_id = app_user() OR {STAFF}", "INSERT_ONLY"),
    "workflows": (
        f"{STAFF} OR owner_user_id = app_user() OR app_is_exhibitor(owner_exhibitor_id)",
        f"{STAFF} OR owner_user_id = app_user() OR app_is_exhibitor(owner_exhibitor_id)",
    ),
    "approvals": (
        f"{STAFF} OR (required_role = 'exhibitor' AND app_is_exhibitor(approver_exhibitor_id))",
        f"{STAFF} OR (required_role = 'exhibitor' AND app_is_exhibitor(approver_exhibitor_id))",
    ),
    "approval_policies": ("true", STAFF),
    "audit_logs": (STAFF, "INSERT_ONLY"),
    "llm_usage_logs": (STAFF, "INSERT_ONLY"),
}

# tables that are not event-scoped: full predicates (no SCOPE prefix)
OTHER_TABLES: dict[str, tuple[str, str | None]] = {
    "tenants": ("id = app_tenant()", None),
    "events": (
        f"tenant_id = app_tenant() AND (id = app_event() OR {STAFF})",
        f"tenant_id = app_tenant() AND id = app_event() AND {STAFF}",
    ),
    # contact details: yourself, staff, or an exhibitor that holds you as a lead
    "users": (
        f"""tenant_id = app_tenant() AND (
              id = app_user() OR {STAFF}
              OR EXISTS (SELECT 1 FROM leads l WHERE l.attendee_user_id = users.id
                         AND l.event_id = app_event() AND l.exhibitor_id = app_exhibitor()
                         AND app_role() = 'exhibitor')
              OR EXISTS (SELECT 1 FROM connections c WHERE c.event_id = app_event()
                         AND c.status = 'accepted'
                         AND ((c.requester_id = app_user() AND c.addressee_id = users.id)
                           OR (c.addressee_id = app_user() AND c.requester_id = users.id))))""",
        f"tenant_id = app_tenant() AND (id = app_user() OR {STAFF})",
    ),
}

AUTH_FUNCTIONS = """
-- Login runs before any request context exists, so these two run as the owner.
CREATE OR REPLACE FUNCTION auth_memberships(p_email text)
RETURNS TABLE (user_id uuid, full_name text, tenant_id uuid, tenant_name text, event_id uuid,
               event_name text, role text, exhibitor_id uuid, exhibitor_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT u.id, u.full_name, t.id, t.name, e.id, e.name, r.role, x.id, x.name
  FROM users u
  JOIN tenants t ON t.id = u.tenant_id
  JOIN user_event_roles r ON r.user_id = u.id
  JOIN events e ON e.id = r.event_id
  LEFT JOIN exhibitors x ON x.id = r.exhibitor_id
  WHERE lower(u.email) = lower(p_email)
  ORDER BY t.name, e.starts_on
$$;

CREATE OR REPLACE FUNCTION auth_demo_personas()
RETURNS TABLE (email text, persona_label text, full_name text, tenant_name text, event_id uuid,
               event_name text, role text, exhibitor_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT u.email, u.persona_label, u.full_name, t.name, e.id, e.name, r.role, x.name
  FROM users u
  JOIN tenants t ON t.id = u.tenant_id
  JOIN user_event_roles r ON r.user_id = u.id
  JOIN events e ON e.id = r.event_id
  LEFT JOIN exhibitors x ON x.id = r.exhibitor_id
  WHERE u.is_demo_persona
  ORDER BY t.name, e.name, r.role, u.full_name
$$;

REVOKE ALL ON FUNCTION auth_memberships(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_demo_personas() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_memberships(text) TO app_rw;
GRANT EXECUTE ON FUNCTION auth_demo_personas() TO app_rw;
"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _roles_sql() -> str:
    s = get_settings()
    rw = _sql_literal(s.app_rw_password.get_secret_value())
    ro = _sql_literal(s.app_readonly_password.get_secret_value())
    return f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN CREATE ROLE app_rw; END IF;
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_readonly') THEN
        CREATE ROLE app_readonly;
      END IF;
    END $$;
    -- new roles start NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE, and only a superuser
    -- may even mention SUPERUSER/BYPASSRLS here, which a managed-Postgres owner is not
    -- (tests/test_isolation.py asserts both roles stay unprivileged)
    ALTER ROLE app_rw WITH LOGIN PASSWORD {rw};
    ALTER ROLE app_readonly WITH LOGIN PASSWORD {ro};
    ALTER ROLE app_readonly SET default_transaction_read_only = on;
    ALTER ROLE app_readonly SET statement_timeout = '5s';
    ALTER ROLE app_rw SET statement_timeout = '15s';
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    GRANT USAGE ON SCHEMA public TO app_rw, app_readonly;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw;
    REVOKE ALL ON alembic_version FROM app_rw;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw;
    """


def _policies(table: str, read: str, write: str | None, scoped: bool) -> list[str]:
    def wrap(pred: str) -> str:
        return f"({SCOPE}) AND ({pred})" if scoped else f"({pred})"

    stmts = [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_read ON {table} FOR SELECT USING ({wrap(read)})",
    ]
    if write == "INSERT_ONLY":
        # any authenticated role may append rows, but only inside its own tenant/event
        stmts.append(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT "
            f"WITH CHECK ({SCOPE} AND app_role() IS NOT NULL)"
        )
    elif write is not None:
        w = wrap(write)
        stmts += [
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({w})",
            f"CREATE POLICY {table}_update ON {table} FOR UPDATE USING ({w}) WITH CHECK ({w})",
            f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({w})",
        ]
    return stmts


ALL_TABLES = {**OTHER_TABLES, **EVENT_TABLES}


def upgrade() -> None:
    exec_script(CONTEXT_FUNCTIONS)
    exec_script(_roles_sql())
    for table, (read, write) in OTHER_TABLES.items():
        for stmt in _policies(table, read, write, scoped=False):
            op.execute(stmt)
    for table, (read, write) in EVENT_TABLES.items():
        for stmt in _policies(table, read, write, scoped=True):
            op.execute(stmt)
    exec_script(AUTH_FUNCTIONS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_memberships(text), auth_demo_personas()")
    for table in ALL_TABLES:
        op.execute(
            f"""DO $$ DECLARE p record; BEGIN
                FOR p IN SELECT policyname FROM pg_policies WHERE tablename = '{table}' LOOP
                  EXECUTE format('DROP POLICY %I ON {table}', p.policyname);
                END LOOP; END $$"""
        )
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_rw")
