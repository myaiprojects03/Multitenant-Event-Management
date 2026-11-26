"""Matchmaking relevance: precision@5 vs declared-interest labels (golden/matching.yaml)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import apply_context
from app.core.db import get_engine
from app.modules.matchmaking import service as mm
from evals.harness import Harness, Login, load
from evals.results import CaseResult, SuiteResult

# relevance of a candidate, decided from declared data only (never from the ranker's score)
LABELS = {
    "session": "SELECT tags && :mine FROM sessions WHERE id = :id",
    "speaker": "SELECT topics && :mine FROM speakers WHERE id = :id",
    "exhibitor": "SELECT tags && :mine FROM exhibitors WHERE id = :id",
    "peer": "SELECT interests && :mine FROM attendee_profiles WHERE id = :id",
    "prospect": "SELECT interests && :mine FROM attendee_profiles WHERE id = :id",
}
GENERATORS = {
    "peers": mm.peers,
    "sessions": mm.sessions,
    "speakers": mm.speakers,
    "exhibitors": mm.exhibitors,
}


async def _ranked_and_pool(login: Login, rec_type: str, k: int):
    """(top-k recommendations, every eligible candidate) in a rolled-back transaction."""
    async with get_engine().connect() as conn:
        trans = await conn.begin()
        try:
            await apply_context(conn, login.ctx)
            db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            ranked = await mm.MatchmakingService(None, None).recommend(
                db, login.ctx, rec_type, k, phrase=False
            )
            if rec_type == "prospects":
                pool = await mm.prospects(db, login.ctx)
            else:
                me = await mm._me(db)
                pool = await GENERATORS[rec_type](db, me) if me else []
            await db.close()
        finally:
            await trans.rollback()
    return ranked.items, pool


async def _mine(h: Harness, login: Login) -> list[str]:
    if login.ctx.role == "exhibitor":
        rows = await h.rows(
            "SELECT tags FROM exhibitors WHERE id = :x", {"x": login.ctx.exhibitor_id}
        )
    else:
        rows = await h.rows(
            "SELECT interests FROM attendee_profiles WHERE user_id = :u AND event_id = :e",
            {"u": login.ctx.user_id, "e": login.ctx.event_id},
        )
    return list(next(iter(rows[0].values()))) if rows else []


async def _relevant(h: Harness, target_type: str, target_id, mine: list[str]) -> bool:
    rows = await h.rows(LABELS[target_type], {"id": str(target_id), "mine": mine})
    return bool(rows and next(iter(rows[0].values())))


async def _subjects(h: Harness, spec: dict) -> list[tuple[str, str | None, list[str]]]:
    out = []
    for s in spec["subjects"]:
        if "as" in s:
            out.append((s["as"], s.get("event"), s["types"]))
            continue
        rows = await h.rows(
            "SELECT u.email FROM attendee_profiles p JOIN users u ON u.id = p.user_id "
            "JOIN events e ON e.id = p.event_id JOIN user_event_roles m ON m.user_id = u.id "
            "AND m.event_id = e.id AND m.role = 'attendee' "
            "WHERE e.name LIKE :e AND p.networking_opt_in AND p.exhibitor_matching_opt_in "
            "AND u.email NOT LIKE 'sara@%' AND u.email NOT LIKE 'leo@%' "
            "ORDER BY md5(u.email) LIMIT :n",
            {"e": s["event"] + "%", "n": s["sample_attendees"]},
        )
        out += [(r["email"], s["event"], s["types"]) for r in rows]
    return out


async def run(h: Harness, n: int | None = None, pace_s: float = 0.0) -> SuiteResult:
    spec = load("matching.yaml")
    k = spec.get("k", 5)
    suite = SuiteResult("matching")
    per_type: dict[str, list[tuple[float, float]]] = {}
    subjects = await _subjects(h, spec)
    if n:
        subjects = subjects[: max(2, n)]
    for who, event, types in subjects:
        login = await h.login(who, event)
        mine = await _mine(h, login)
        for t in types:
            top, pool = await _ranked_and_pool(login, t, k)
            if not top:
                suite.cases.append(CaseResult(f"{who}:{t}", False, "no recommendations"))
                continue
            rel = [await _relevant(h, i.target_type, i.target_id, mine) for i in top[:k]]
            pool_rel = [await _relevant(h, c.target_type, c.target_id, mine) for c in pool]
            p_at_k = sum(rel) / len(rel)
            base = sum(pool_rel) / len(pool_rel) if pool_rel else 0.0
            per_type.setdefault(t, []).append((p_at_k, base))
            # per-subject rows are informative; the target is on the average (see report)
            note = "" if p_at_k >= base else " (below base rate)"
            suite.cases.append(
                CaseResult(
                    f"{who.split('@')[0]}:{t}",
                    True,
                    f"P@{k} {p_at_k:.2f} vs base rate {base:.2f} ({len(pool)} candidates){note}",
                    extra={"p": p_at_k, "base": base},
                )
            )

    for m in spec.get("must_include", []):
        login = await h.login(m["as"], m.get("event"))
        top, _ = await _ranked_and_pool(login, m["type"], k)
        want = set(await h.values(m["any_of"], login))
        got = [str(i.target_id) for i in top[:k]]
        ok = bool(want & set(got))
        suite.cases.append(
            CaseResult(
                f"must:{m['id']}",
                ok,
                "found in top 5" if ok else "none of the labelled items in top 5",
            )
        )

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    suite.metrics = {
        "precision_at_5": avg([p for v in per_type.values() for p, _ in v]),
        "base_rate": avg([b for v in per_type.values() for _, b in v]),
        "by_type": {
            t: {"p_at_5": avg([p for p, _ in v]), "base": avg([b for _, b in v]), "n": len(v)}
            for t, v in per_type.items()
        },
        "must_include": f"{sum(c.passed for c in suite.cases if c.id.startswith('must:'))}"
        f"/{len(spec.get('must_include', []))}",
        "subjects": len(subjects),
        "below_base_rate": sum("below base" in c.detail for c in suite.cases),
    }
    suite.metrics["lift"] = (
        suite.metrics["precision_at_5"] / suite.metrics["base_rate"]
        if suite.metrics["base_rate"]
        else None
    )
    return suite
