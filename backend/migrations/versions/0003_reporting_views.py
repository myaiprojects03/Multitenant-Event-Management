"""Reporting views (BI copilot) and the anonymized exhibitor prospects view.

These views run with the view owner's rights so that `app_readonly` needs SELECT on the views
only, never on base tables. Because the owner bypasses RLS, every view filters on the request
context itself: tenant + event from app_tenant()/app_event(), plus a role check. With no context
set, each view returns zero rows.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAFF_SCOPE = "{a}.tenant_id = app_tenant() AND {a}.event_id = app_event() AND app_is_staff()"

REPORTING_VIEWS: dict[str, str] = {
    # one row per session: demand (bookmarks vs capacity), turnout, and the drop-off pattern
    "session_attendance_v": f"""
WITH presence AS (
  SELECT session_id, user_id,
         min(occurred_at) FILTER (WHERE kind = 'session_check_in')  AS in_at,
         max(occurred_at) FILTER (WHERE kind = 'session_check_out') AS out_at
  FROM engagement_logs g
  WHERE {STAFF_SCOPE.format(a="g")}
    AND kind IN ('session_check_in', 'session_check_out')
  GROUP BY session_id, user_id
), bm AS (
  SELECT session_id, count(*) AS n FROM bookmarks b
  WHERE {STAFF_SCOPE.format(a="b")} GROUP BY session_id
)
SELECT s.tenant_id, s.event_id, s.id AS session_id, s.title, s.kind AS session_kind,
       t.name AS track, r.name AS room, s.starts_at, s.ends_at,
       (extract(epoch FROM s.ends_at - s.starts_at) / 60)::int AS duration_min,
       s.capacity,
       coalesce(bm.n, 0) AS bookmarks,
       round(100.0 * coalesce(bm.n, 0) / nullif(s.capacity, 0), 1) AS bookmark_capacity_pct,
       count(p.in_at) AS checkins,
       round(100.0 * count(p.in_at) / nullif(s.capacity, 0), 1) AS checkin_capacity_pct,
       round(avg(extract(epoch FROM least(coalesce(p.out_at, s.ends_at), s.ends_at)
                 - greatest(p.in_at, s.starts_at)) / 60)::numeric, 1) AS avg_minutes_attended,
       count(*) FILTER (WHERE p.in_at <= s.starts_at + interval '20 min'
                          AND coalesce(p.out_at, s.ends_at) > s.starts_at + interval '20 min')
         AS present_at_20min,
       round(100.0 * count(*) FILTER (
               WHERE p.in_at <= s.starts_at + interval '20 min'
                 AND p.out_at > s.starts_at + interval '20 min'
                 AND p.out_at < s.ends_at - interval '5 min')
             / nullif(count(*) FILTER (
               WHERE p.in_at <= s.starts_at + interval '20 min'
                 AND coalesce(p.out_at, s.ends_at) > s.starts_at + interval '20 min'), 0), 1)
         AS dropoff_after_20min_pct,
       round(100.0 * count(*) FILTER (WHERE p.out_at < s.ends_at - interval '5 min')
             / nullif(count(p.in_at), 0), 1) AS early_exit_pct
FROM sessions s
LEFT JOIN tracks t ON t.id = s.track_id
LEFT JOIN rooms r ON r.id = s.room_id
LEFT JOIN bm ON bm.session_id = s.id
LEFT JOIN presence p ON p.session_id = s.id AND p.in_at IS NOT NULL
WHERE {STAFF_SCOPE.format(a="s")}
GROUP BY s.id, t.name, r.name, bm.n
""",
    # one row per exhibitor per day
    "lead_summary_v": f"""
SELECT l.tenant_id, l.event_id, x.id AS exhibitor_id, x.name AS exhibitor_name,
       x.category AS exhibitor_category, x.booth,
       (l.captured_at AT TIME ZONE 'UTC')::date AS lead_date,
       count(*) AS leads,
       count(*) FILTER (WHERE l.tier IN ('A', 'B')) AS qualified_leads,
       count(*) FILTER (WHERE l.score IS NULL) AS unscored_leads,
       round(avg(l.score), 1) AS avg_score,
       count(*) FILTER (WHERE l.source = 'badge_scan') AS badge_scan_leads,
       count(*) FILTER (WHERE l.source = 'meeting_accepted') AS meeting_leads
FROM leads l
JOIN exhibitors x ON x.id = l.exhibitor_id
WHERE {STAFF_SCOPE.format(a="l")}
GROUP BY l.tenant_id, l.event_id, x.id, (l.captured_at AT TIME ZONE 'UTC')::date
""",
    # one row per exhibitor (whole event)
    "exhibitor_performance_v": f"""
WITH visits AS (
  SELECT exhibitor_id, count(*) AS booth_visits, count(DISTINCT user_id) AS unique_visitors
  FROM engagement_logs g
  WHERE {STAFF_SCOPE.format(a="g")} AND kind = 'booth_visit' GROUP BY exhibitor_id
), lds AS (
  SELECT exhibitor_id, count(*) AS leads,
         count(*) FILTER (WHERE tier IN ('A', 'B')) AS qualified_leads,
         round(avg(score), 1) AS avg_score
  FROM leads l WHERE {STAFF_SCOPE.format(a="l")} GROUP BY exhibitor_id
), mtg AS (
  SELECT exhibitor_id, count(*) AS meeting_requests,
         count(*) FILTER (WHERE status = 'accepted') AS meetings_accepted
  FROM meeting_requests m WHERE {STAFF_SCOPE.format(a="m")} GROUP BY exhibitor_id
)
SELECT x.tenant_id, x.event_id, x.id AS exhibitor_id, x.name AS exhibitor_name,
       x.category AS exhibitor_category, x.booth,
       coalesce(v.booth_visits, 0) AS booth_visits,
       coalesce(v.unique_visitors, 0) AS unique_visitors,
       coalesce(l.leads, 0) AS leads,
       coalesce(l.qualified_leads, 0) AS qualified_leads,
       l.avg_score,
       round(100.0 * coalesce(l.leads, 0) / nullif(v.unique_visitors, 0), 1)
         AS visitor_to_lead_pct,
       coalesce(m.meeting_requests, 0) AS meeting_requests,
       coalesce(m.meetings_accepted, 0) AS meetings_accepted
FROM exhibitors x
LEFT JOIN visits v ON v.exhibitor_id = x.id
LEFT JOIN lds l ON l.exhibitor_id = x.id
LEFT JOIN mtg m ON m.exhibitor_id = x.id
WHERE {STAFF_SCOPE.format(a="x")}
""",
    # one row per hour per engagement kind
    "engagement_v": f"""
SELECT g.tenant_id, g.event_id,
       date_trunc('hour', g.occurred_at) AS hour,
       (g.occurred_at AT TIME ZONE 'UTC')::date AS day,
       g.kind,
       count(*) AS events,
       count(DISTINCT g.user_id) AS unique_users
FROM engagement_logs g
WHERE {STAFF_SCOPE.format(a="g")}
GROUP BY g.tenant_id, g.event_id, date_trunc('hour', g.occurred_at),
         (g.occurred_at AT TIME ZONE 'UTC')::date, g.kind
""",
    # one row per day: peer networking and exhibitor meetings
    "networking_v": f"""
WITH c AS (
  SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS connection_requests,
         count(*) FILTER (WHERE status = 'accepted') AS connections_accepted
  FROM connections c WHERE {STAFF_SCOPE.format(a="c")} GROUP BY 1
), m AS (
  SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS meeting_requests,
         count(*) FILTER (WHERE status = 'accepted') AS meetings_accepted
  FROM meeting_requests m WHERE {STAFF_SCOPE.format(a="m")} GROUP BY 1
)
SELECT app_tenant() AS tenant_id, app_event() AS event_id,
       coalesce(c.day, m.day) AS day,
       coalesce(c.connection_requests, 0) AS connection_requests,
       coalesce(c.connections_accepted, 0) AS connections_accepted,
       round(100.0 * c.connections_accepted / nullif(c.connection_requests, 0), 1)
         AS connection_accept_pct,
       coalesce(m.meeting_requests, 0) AS meeting_requests,
       coalesce(m.meetings_accepted, 0) AS meetings_accepted
FROM c FULL JOIN m ON m.day = c.day
""",
    # one row per attendee-facing opt-in mix (useful for networking reach questions)
    "attendee_optin_v": f"""
SELECT p.tenant_id, p.event_id, coalesce(p.industry, 'unknown') AS industry,
       count(*) AS attendees,
       count(*) FILTER (WHERE p.networking_opt_in) AS networking_opt_in,
       count(*) FILTER (WHERE p.exhibitor_matching_opt_in) AS exhibitor_matching_opt_in
FROM attendee_profiles p
WHERE {STAFF_SCOPE.format(a="p")}
GROUP BY p.tenant_id, p.event_id, coalesce(p.industry, 'unknown')
""",
}

# Exhibitors browse consenting attendees without identity: no name, company, email or user id.
PROSPECTS_VIEW = """
CREATE VIEW exhibitor_prospects_v WITH (security_barrier) AS
SELECT p.tenant_id, p.event_id,
       p.id AS profile_id,
       'P-' || upper(substr(md5(p.id::text || app_exhibitor()::text), 1, 6)) AS prospect_ref,
       p.industry, p.seniority, p.persona_type, p.interests, p.looking_for,
       EXISTS (SELECT 1 FROM leads l WHERE l.attendee_user_id = p.user_id
               AND l.exhibitor_id = app_exhibitor()) AS is_lead
FROM attendee_profiles p
WHERE p.tenant_id = app_tenant() AND p.event_id = app_event()
  AND app_role() = 'exhibitor' AND app_exhibitor() IS NOT NULL
  AND p.exhibitor_matching_opt_in
"""

REPORTING_VIEW_NAMES = tuple(REPORTING_VIEWS)


def upgrade() -> None:
    for name, body in REPORTING_VIEWS.items():
        op.execute(f"CREATE VIEW {name} WITH (security_barrier) AS {body}")
        op.execute(f"GRANT SELECT ON {name} TO app_readonly, app_rw")
    op.execute(PROSPECTS_VIEW)
    op.execute("GRANT SELECT ON exhibitor_prospects_v TO app_rw")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS exhibitor_prospects_v")
    for name in reversed(REPORTING_VIEW_NAMES):
        op.execute(f"DROP VIEW IF EXISTS {name}")
