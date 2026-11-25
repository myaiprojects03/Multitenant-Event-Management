"""Agents + HITL: outbound action outbox, workflow history, sweeper function.

outbound_actions is the only place an external effect (email, platform API call, publish) can
come from. RLS makes "nothing executes without approval" a database rule, not just app code:
a row can be inserted only by staff/system, only for a workflow whose every approval is
approved, and it must reference one of those approvals. Exhibitors can read their own
actions (delivery status); nobody but staff/system can change them.

workflow_due_work() lets the worker's sweeper find work across events without owner
credentials. It returns ids only, and only on a connection with no request context (a user
request always has one).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.sqlutil import exec_script

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE = "tenant_id = app_tenant() AND event_id = app_event()"

RLS = f"""
ALTER TABLE outbound_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbound_actions FORCE ROW LEVEL SECURITY;
CREATE POLICY outbound_actions_read ON outbound_actions FOR SELECT
  USING ({SCOPE} AND (app_is_staff() OR app_is_exhibitor(owner_exhibitor_id)));
CREATE POLICY outbound_actions_insert ON outbound_actions FOR INSERT WITH CHECK (
  {SCOPE} AND app_is_staff()
  AND EXISTS (SELECT 1 FROM approvals a WHERE a.id = outbound_actions.approval_id
              AND a.workflow_id = outbound_actions.workflow_id AND a.status = 'approved')
  AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.workflow_id = outbound_actions.workflow_id
                  AND a.status <> 'approved')
);
CREATE POLICY outbound_actions_update ON outbound_actions FOR UPDATE
  USING ({SCOPE} AND app_is_staff()) WITH CHECK ({SCOPE} AND app_is_staff());

GRANT SELECT, INSERT, UPDATE ON outbound_actions TO app_rw;
"""

FUNCTIONS = """
CREATE OR REPLACE FUNCTION workflow_due_work()
RETURNS TABLE (tenant_id uuid, event_id uuid, workflow_id uuid, work text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT * FROM (
    SELECT w.tenant_id, w.event_id, w.id, 'run' FROM workflows w
     WHERE w.status = 'pending' AND w.created_at < now() - interval '15 seconds'
    UNION
    SELECT a.tenant_id, a.event_id, a.workflow_id, 'expire' FROM approvals a
     WHERE a.status = 'pending' AND a.expires_at < now()
    UNION
    SELECT o.tenant_id, o.event_id, o.workflow_id, 'deliver' FROM outbound_actions o
     WHERE o.status IN ('queued', 'retrying') AND o.next_attempt_at <= now()
  ) due
  WHERE app_role() IS NULL
$$;
REVOKE ALL ON FUNCTION workflow_due_work() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION workflow_due_work() TO app_rw;
"""


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column(
            "history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_approvals_workflow_level", "approvals", ["workflow_id", "escalation_level"]
    )
    op.create_table(
        "outbound_actions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=False),
        sa.Column("owner_exhibitor_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("stage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient_user_id", sa.UUID(), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'platform_api', 'publish')", name="channel_valid"),
        sa.CheckConstraint(
            "status IN ('queued', 'retrying', 'sent', 'dead_letter', 'cancelled')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_exhibitor_id"], ["exhibitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_outbound_actions_scope", "outbound_actions", ["tenant_id", "event_id"], unique=False
    )
    op.create_index(
        "ix_outbound_actions_workflow", "outbound_actions", ["workflow_id", "stage"], unique=False
    )
    exec_script(RLS)
    exec_script(FUNCTIONS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS workflow_due_work()")
    op.drop_table("outbound_actions")
    op.drop_constraint("uq_approvals_workflow_level", "approvals", type_="unique")
    op.drop_column("workflows", "history")
