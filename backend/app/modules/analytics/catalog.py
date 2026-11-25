"""The only data the BI copilot may query: approved reporting views and their columns.

tenant_id / event_id are deliberately absent: the views scope themselves to the caller's
event, so the model never needs (and may not use) them.
"""

VIEWS: dict[str, dict] = {
    "session_attendance_v": {
        "about": "One row per session: demand, turnout and drop-off.",
        "columns": {
            "session_id": "uuid",
            "title": "text",
            "session_kind": "text: keynote | talk | panel | workshop",
            "track": "text",
            "room": "text",
            "starts_at": "timestamptz (UTC)",
            "ends_at": "timestamptz (UTC)",
            "duration_min": "int",
            "capacity": "int, seats in the room",
            "bookmarks": "int, attendees who saved the session",
            "bookmark_capacity_pct": "numeric, bookmarks / capacity * 100 (>100 = overbooked)",
            "checkins": "int, attendees who checked in",
            "checkin_capacity_pct": "numeric",
            "avg_minutes_attended": "numeric",
            "present_at_20min": "int, attendees still in the room at minute 20",
            "dropoff_after_20min_pct": "numeric, % of those present at minute 20 who left early",
            "early_exit_pct": "numeric, % of check-ins who left more than 5 min before the end",
        },
    },
    "lead_summary_v": {
        "about": "One row per exhibitor per day of captured leads.",
        "columns": {
            "exhibitor_id": "uuid",
            "exhibitor_name": "text",
            "exhibitor_category": "text",
            "booth": "text",
            "lead_date": "date (UTC)",
            "leads": "int",
            "qualified_leads": "int, leads in tier A or B",
            "unscored_leads": "int",
            "avg_score": "numeric 0-100",
            "badge_scan_leads": "int",
            "meeting_leads": "int",
        },
    },
    "exhibitor_performance_v": {
        "about": "One row per exhibitor for the whole event.",
        "columns": {
            "exhibitor_id": "uuid",
            "exhibitor_name": "text",
            "exhibitor_category": "text",
            "booth": "text",
            "booth_visits": "int",
            "unique_visitors": "int",
            "leads": "int",
            "qualified_leads": "int",
            "avg_score": "numeric",
            "visitor_to_lead_pct": "numeric",
            "meeting_requests": "int",
            "meetings_accepted": "int",
        },
    },
    "engagement_v": {
        "about": "One row per hour per engagement kind.",
        "columns": {
            "hour": "timestamptz, start of the hour (UTC)",
            "day": "date (UTC)",
            "kind": "text: session_check_in | session_check_out | booth_visit | click | app_open",
            "events": "int, number of events",
            "unique_users": "int",
        },
    },
    "networking_v": {
        "about": "One row per day of peer networking and exhibitor meetings.",
        "columns": {
            "day": "date (UTC)",
            "connection_requests": "int",
            "connections_accepted": "int",
            "connection_accept_pct": "numeric",
            "meeting_requests": "int",
            "meetings_accepted": "int",
        },
    },
    "attendee_optin_v": {
        "about": "One row per attendee industry: consent mix.",
        "columns": {
            "industry": "text",
            "attendees": "int",
            "networking_opt_in": "int, attendees opted in to peer networking",
            "exhibitor_matching_opt_in": "int, attendees opted in to exhibitor matching",
        },
    },
}

ALLOWED_COLUMNS: dict[str, frozenset[str]] = {
    name: frozenset(view["columns"]) for name, view in VIEWS.items()
}


def schema_prompt() -> str:
    parts = []
    for name, view in VIEWS.items():
        cols = "\n".join(f"    {c}: {t}" for c, t in view["columns"].items())
        parts.append(f"{name}  -- {view['about']}\n{cols}")
    return "\n\n".join(parts)
