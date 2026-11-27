"""RLS binds the app roles, not the owner: NO FORCE ROW LEVEL SECURITY.

The design always was: the owner (migrations, seed, SECURITY DEFINER functions such as
exhibitor_prospect_matches and workflow_due_work) works across tenants; request code connects
as app_rw / app_readonly, which are not owners and have NOBYPASSRLS, so every policy applies.

FORCE made the owner obey RLS too. That only went unnoticed locally because the Docker owner
is a superuser (superusers skip RLS regardless). On managed Postgres (Render) the owner is an
ordinary role: with FORCE the seed and the SECURITY DEFINER functions are blocked. Dropping
FORCE restores the intended split; nothing changes for the app roles.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from migrations.sqlutil import exec_script

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EACH_TABLE = """
DO $$ DECLARE t record; BEGIN
  FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity LOOP
    EXECUTE format('ALTER TABLE %I {mode} FORCE ROW LEVEL SECURITY', t.relname);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    exec_script(_EACH_TABLE.format(mode="NO"))


def downgrade() -> None:
    exec_script(_EACH_TABLE.format(mode=""))
