"""Matchmaking: profile vectors, feedback events, and consent-safe SQL functions.

RLS
* profile_vectors: non-attendee vectors are readable in the event; an attendee vector is
  readable only where the attendee's profile is (the policy defers to attendee_profiles RLS,
  so networking consent carries over automatically). Writes: staff/system only.
* recommendation_events: each user reads and appends only their own feedback.

SECURITY DEFINER functions. These run as the owner, so each one checks the caller's
context itself and returns the minimum:
* exhibitor_prospect_matches: anonymized, exhibitor-matching-opted-in prospects ranked by
  vector similarity to the calling exhibitor. No names, companies or user ids leave.
* peer_session_overlap: count of shared bookmarks with networking-opted-in peers only.
* request_meeting_with_prospect: exhibitor asks an anonymized prospect for a meeting.
* respond_meeting: the receiving party accepts/declines; acceptance creates the lead.
* badge_scan: exhibitor scans an attendee badge in this event; that creates the lead.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import pgvector.sqlalchemy.vector
import sqlalchemy as sa
from alembic import op

from migrations.sqlutil import exec_script

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE = "tenant_id = app_tenant() AND event_id = app_event()"

RLS = f"""
ALTER TABLE profile_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY profile_vectors_read ON profile_vectors FOR SELECT USING (
  {SCOPE} AND (
    entity_type <> 'attendee' OR app_is_staff()
    OR EXISTS (SELECT 1 FROM attendee_profiles p WHERE p.id = profile_vectors.entity_id)
  )
);
CREATE POLICY profile_vectors_insert ON profile_vectors FOR INSERT
  WITH CHECK ({SCOPE} AND app_is_staff());
CREATE POLICY profile_vectors_update ON profile_vectors FOR UPDATE
  USING ({SCOPE} AND app_is_staff()) WITH CHECK ({SCOPE} AND app_is_staff());
CREATE POLICY profile_vectors_delete ON profile_vectors FOR DELETE
  USING ({SCOPE} AND app_is_staff());

ALTER TABLE recommendation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_events FORCE ROW LEVEL SECURITY;
CREATE POLICY recommendation_events_read ON recommendation_events FOR SELECT
  USING ({SCOPE} AND (user_id = app_user() OR app_is_staff()));
CREATE POLICY recommendation_events_insert ON recommendation_events FOR INSERT
  WITH CHECK ({SCOPE} AND user_id = app_user());

GRANT SELECT, INSERT, UPDATE, DELETE ON profile_vectors, recommendation_events TO app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw;
"""

FUNCTIONS = """
CREATE OR REPLACE FUNCTION exhibitor_prospect_matches(p_limit int)
RETURNS TABLE (profile_id uuid, prospect_ref text, industry text, seniority text,
               persona_type text, interests text[], looking_for text[],
               similarity float8, is_lead boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT p.id,
         'P-' || upper(substr(md5(p.id::text || app_exhibitor()::text), 1, 6)),
         p.industry, p.seniority, p.persona_type, p.interests, p.looking_for,
         1 - (pv.embedding <=> xv.embedding),
         EXISTS (SELECT 1 FROM leads l WHERE l.attendee_user_id = p.user_id
                 AND l.exhibitor_id = app_exhibitor() AND l.event_id = p.event_id)
  FROM attendee_profiles p
  JOIN profile_vectors pv ON pv.event_id = p.event_id AND pv.entity_type = 'attendee'
                         AND pv.entity_id = p.id
  JOIN profile_vectors xv ON xv.event_id = p.event_id AND xv.entity_type = 'exhibitor'
                         AND xv.entity_id = app_exhibitor()
  WHERE app_role() = 'exhibitor' AND app_exhibitor() IS NOT NULL
    AND p.tenant_id = app_tenant() AND p.event_id = app_event()
    AND p.exhibitor_matching_opt_in
  ORDER BY pv.embedding <=> xv.embedding
  LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

CREATE OR REPLACE FUNCTION peer_session_overlap(p_users uuid[])
RETURNS TABLE (user_id uuid, shared int)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT b.user_id, count(*)::int
  FROM bookmarks b
  WHERE app_role() = 'attendee'
    AND b.tenant_id = app_tenant() AND b.event_id = app_event()
    AND b.user_id = ANY (p_users) AND b.user_id <> app_user()
    AND EXISTS (SELECT 1 FROM attendee_profiles p WHERE p.user_id = b.user_id
                AND p.event_id = app_event() AND p.networking_opt_in)
    AND b.session_id IN (SELECT m.session_id FROM bookmarks m
                         WHERE m.user_id = app_user() AND m.event_id = app_event())
  GROUP BY b.user_id
$$;

CREATE OR REPLACE FUNCTION request_meeting_with_prospect(p_profile uuid, p_message text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_user uuid; v_id uuid;
BEGIN
  IF app_role() IS DISTINCT FROM 'exhibitor' OR app_exhibitor() IS NULL THEN
    RAISE EXCEPTION 'only exhibitors can request prospect meetings';
  END IF;
  SELECT p.user_id INTO v_user FROM attendee_profiles p
   WHERE p.id = p_profile AND p.tenant_id = app_tenant() AND p.event_id = app_event()
     AND p.exhibitor_matching_opt_in;
  IF v_user IS NULL THEN RAISE EXCEPTION 'prospect not available'; END IF;
  SELECT m.id INTO v_id FROM meeting_requests m
   WHERE m.event_id = app_event() AND m.exhibitor_id = app_exhibitor()
     AND m.attendee_user_id = v_user AND m.status IN ('pending', 'accepted');
  IF v_id IS NOT NULL THEN RETURN v_id; END IF;
  INSERT INTO meeting_requests (tenant_id, event_id, attendee_user_id, exhibitor_id,
                                initiated_by, status, message)
  VALUES (app_tenant(), app_event(), v_user, app_exhibitor(), 'exhibitor', 'pending',
          left(p_message, 500))
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION respond_meeting(p_meeting uuid, p_accept boolean)
RETURNS TABLE (status text, lead_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE m meeting_requests%ROWTYPE; v_lead uuid; v_status text;
BEGIN
  SELECT * INTO m FROM meeting_requests r
   WHERE r.id = p_meeting AND r.tenant_id = app_tenant() AND r.event_id = app_event()
   FOR UPDATE;
  IF m.id IS NULL THEN RAISE EXCEPTION 'meeting not found'; END IF;
  -- only the receiving party may respond
  IF m.initiated_by = 'exhibitor' THEN
    IF app_role() IS DISTINCT FROM 'attendee' OR m.attendee_user_id <> app_user() THEN
      RAISE EXCEPTION 'meeting not found';
    END IF;
  ELSE
    IF app_role() IS DISTINCT FROM 'exhibitor' OR m.exhibitor_id <> app_exhibitor() THEN
      RAISE EXCEPTION 'meeting not found';
    END IF;
  END IF;
  IF m.status <> 'pending' THEN RAISE EXCEPTION 'meeting already %', m.status; END IF;
  v_status := CASE WHEN p_accept THEN 'accepted' ELSE 'declined' END;
  UPDATE meeting_requests SET status = v_status, responded_at = now() WHERE id = m.id;
  IF p_accept THEN
    -- the consent moment: the attendee agreed to meet, so a lead now exists
    INSERT INTO leads (tenant_id, event_id, exhibitor_id, attendee_user_id, source, captured_at)
    VALUES (m.tenant_id, m.event_id, m.exhibitor_id, m.attendee_user_id, 'meeting_accepted',
            now())
    ON CONFLICT (exhibitor_id, attendee_user_id) DO NOTHING;
    SELECT l.id INTO v_lead FROM leads l
     WHERE l.exhibitor_id = m.exhibitor_id AND l.attendee_user_id = m.attendee_user_id;
  END IF;
  INSERT INTO audit_logs (tenant_id, event_id, actor_user_id, actor_role, action,
                          target_type, target_id, details)
  VALUES (m.tenant_id, m.event_id, app_user(), app_role(), 'meeting.' || v_status,
          'meeting_request', m.id::text, jsonb_build_object('lead_id', v_lead));
  RETURN QUERY SELECT v_status, v_lead;
END $$;

CREATE OR REPLACE FUNCTION badge_scan(p_user uuid)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_lead uuid;
BEGIN
  IF app_role() IS DISTINCT FROM 'exhibitor' OR app_exhibitor() IS NULL THEN
    RAISE EXCEPTION 'only exhibitors can scan badges';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM user_event_roles r WHERE r.user_id = p_user
                 AND r.event_id = app_event() AND r.tenant_id = app_tenant()
                 AND r.role = 'attendee') THEN
    RAISE EXCEPTION 'badge not valid for this event';
  END IF;
  INSERT INTO leads (tenant_id, event_id, exhibitor_id, attendee_user_id, source, captured_at)
  VALUES (app_tenant(), app_event(), app_exhibitor(), p_user, 'badge_scan', now())
  ON CONFLICT (exhibitor_id, attendee_user_id) DO NOTHING;
  SELECT l.id INTO v_lead FROM leads l
   WHERE l.exhibitor_id = app_exhibitor() AND l.attendee_user_id = p_user;
  INSERT INTO audit_logs (tenant_id, event_id, actor_user_id, actor_role, action,
                          target_type, target_id)
  VALUES (app_tenant(), app_event(), app_user(), 'exhibitor', 'lead.badge_scan', 'lead',
          v_lead::text);
  RETURN v_lead;
END $$;

REVOKE ALL ON FUNCTION exhibitor_prospect_matches(int), peer_session_overlap(uuid[]),
  request_meeting_with_prospect(uuid, text), respond_meeting(uuid, boolean), badge_scan(uuid)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION exhibitor_prospect_matches(int), peer_session_overlap(uuid[]),
  request_meeting_with_prospect(uuid, text), respond_meeting(uuid, boolean), badge_scan(uuid)
  TO app_rw;
"""


def upgrade() -> None:
    op.create_table(
        "profile_vectors",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('attendee', 'session', 'speaker', 'exhibitor')",
            name="entity_type_valid",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "entity_type", "entity_id"),
    )
    op.create_index("ix_profile_vectors_scope", "profile_vectors", ["tenant_id", "event_id"])
    op.create_table(
        "recommendation_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('impression', 'click', 'save', 'connect', 'dismiss')", name="kind_valid"
        ),
        sa.CheckConstraint(
            "target_type IN ('peer', 'session', 'speaker', 'exhibitor', 'prospect')",
            name="target_type_valid",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rec_events_user", "recommendation_events", ["user_id", "target_type", "kind"]
    )
    op.create_index(
        "ix_recommendation_events_scope", "recommendation_events", ["tenant_id", "event_id"]
    )
    exec_script(RLS)
    exec_script(FUNCTIONS)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS exhibitor_prospect_matches(int), peer_session_overlap(uuid[]), "
        "request_meeting_with_prospect(uuid, text), respond_meeting(uuid, boolean), "
        "badge_scan(uuid)"
    )
    op.drop_table("recommendation_events")
    op.drop_table("profile_vectors")
