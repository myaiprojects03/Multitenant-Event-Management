"""Checks shared by the assistant-facing suites."""

import re

from evals.harness import Harness, Login, contains

LOOKUP_TABLES = {
    "room": "rooms",
    "session": "sessions",
    "exhibitor": "exhibitors",
    "speaker": "speakers",
}


def can_see(visibility: str, owner, login: Login) -> bool:
    """Mirror of app_can_see() in 0002_security.py, evaluated outside the database."""
    role = login.ctx.role
    if visibility == "public":
        return True
    if visibility == "attendee":
        return role in ("attendee", "organizer")
    if visibility == "exhibitor_private":
        return role == "organizer" or (
            role == "exhibitor" and owner is not None and owner == login.ctx.exhibitor_id
        )
    if visibility == "organizer_only":
        return role == "organizer"
    return False


async def check_citations(h: Harness, answer: dict, login: Login) -> tuple[int, list[str]]:
    """-> (valid citations, problems). A citation is valid when its source exists in the
    persona's event, the persona's role may see it, and the [n] markers point at real ones."""
    problems: list[str] = []
    cites = answer.get("citations") or []
    refs = {c["ref"] for c in cites}
    for n in {int(m) for m in re.findall(r"\[(\d+)\]", answer.get("answer", ""))}:
        if n not in refs:
            problems.append(f"marker [{n}] has no citation")
    valid = 0
    for c in cites:
        rows = await h.rows(
            "SELECT event_id, visibility, owner_exhibitor_id FROM embeddings "
            "WHERE entity_id = CAST(:id AS uuid) AND entity_type = :t",
            {"id": c["entity_id"], "t": c["entity_type"]},
        )
        if not rows and c["entity_type"] in LOOKUP_TABLES:
            # exact lookups cite the schedule row itself (rooms are not indexed): public data
            rows = await h.rows(
                f"SELECT event_id, 'public' AS visibility, NULL::uuid AS owner_exhibitor_id "
                f"FROM {LOOKUP_TABLES[c['entity_type']]} WHERE id = CAST(:id AS uuid)",
                {"id": c["entity_id"]},
            )
        if not rows:
            problems.append(f"citation {c['ref']} ({c['entity_type']}) does not exist")
            continue
        r = rows[0]
        if r["event_id"] != login.ctx.event_id:
            problems.append(f"citation {c['ref']} is from another event")  # leak
        elif not can_see(r["visibility"], r["owner_exhibitor_id"], login):
            problems.append(f"citation {c['ref']} is {r['visibility']}")  # leak
        else:
            valid += 1
    return valid, problems


def leaked(text_: str, forbid: list[str], regexes: list[str]) -> list[str]:
    hits = [f for f in forbid if f and contains(text_, f)]
    for rx in regexes:
        m = re.search(rx, text_)
        if m:
            hits.append(m.group(0))
    return hits


def answer_text(answer: dict) -> str:
    parts = [answer.get("answer", "")]
    for c in answer.get("citations") or []:
        parts += [c.get("title", ""), c.get("snippet", "")]
    return "\n".join(parts)
