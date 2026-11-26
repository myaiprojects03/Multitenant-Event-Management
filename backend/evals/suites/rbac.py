"""RBAC, isolation, consent and prompt injection (golden/rbac.yaml). Target: zero leaks."""

import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import apply_context
from app.core.db import get_engine
from app.modules.matchmaking.service import MatchmakingService
from evals.harness import Harness, Login, load
from evals.results import CaseResult, SuiteResult, sample
from evals.suites.common import answer_text, check_citations, leaked

SKIP_KINDS: set[str] = set()  # --no-llm skips the kinds that call Gemini


async def recommend_rolled_back(login: Login, rec_type: str, limit: int = 10):
    """Recommendations as the persona, inside a transaction that is rolled back (no
    impressions persist, so evals never change later rankings)."""
    async with get_engine().connect() as conn:
        trans = await conn.begin()
        try:
            await apply_context(conn, login.ctx)
            db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            result = await MatchmakingService(None, None).recommend(
                db, login.ctx, rec_type, limit, phrase=False
            )
            await db.close()
        finally:
            await trans.rollback()
    return result


async def _case(h: Harness, case: dict) -> CaseResult:
    kind = case["kind"]
    cid = case["id"]
    login = await h.login(case["as"], case.get("event")) if case.get("as") else None
    forbid = list(case.get("forbid", []))
    if case.get("forbid_var"):
        forbid += await h.values(case["forbid_var"], login)
    regexes = case.get("forbid_regex", [])

    if kind == "ask":
        status, ans, _ = await h.call(
            "rbac", cid, "POST", "/assistant/query", login=login, body={"question": case["q"]}
        )
        if status != 200:
            return CaseResult(cid, status in (403, 429), f"HTTP {status}", error=status >= 500)
        hits = leaked(answer_text(ans), forbid, regexes)
        _, cite_problems = await check_citations(h, ans, login)
        bad_cites = [p for p in cite_problems if "another event" in p or "is " in p]
        problems = [f"leaked '{x}'" for x in hits] + bad_cites
        return CaseResult(
            cid,
            not problems,
            "; ".join(problems) or f"held ({ans.get('route')})",
            leak=bool(problems),
            extra={"answer": ans.get("answer", "")[:240]},
        )

    if kind == "http":
        token = None
        if case.get("token") == "none":
            token = ""
        elif case.get("token") == "forged":
            token = Harness.forged(login.token)
        status, body, _ = await h.call(
            "rbac",
            cid,
            case["method"],
            case["path"],
            login=login,
            body=case.get("body"),
            token=token,
            timed=False,
        )
        ok = status in case["status"]
        return CaseResult(
            cid,
            ok,
            f"HTTP {status} (expected {case['status']})",
            leak=not ok and 200 <= status < 300,
        )

    if kind == "recs":
        result = await recommend_rolled_back(login, case["type"])
        bad_ids = set(await h.values(case.get("forbid_ids"), login))
        hits = []
        for item in result.items:
            ids = {str(item.target_id), str(item.data.get("user_id", ""))}
            if ids & bad_ids:
                hits.append(f"non-consenting {item.target_type} {item.title}")
        dump = json.dumps([i.model_dump(mode="json") for i in result.items])
        hits += [f"leaked '{x}'" for x in leaked(dump, forbid, regexes)]
        return CaseResult(
            cid, not hits, "; ".join(hits) or f"held ({len(result.items)} items)", leak=bool(hits)
        )

    if kind == "nl_sql":
        status, ans, _ = await h.call(
            "rbac", cid, "POST", "/analytics/nl-sql", login=login, body={"question": case["q"]}
        )
        if status != 200:
            return CaseResult(cid, True, f"refused HTTP {status}")
        hits = leaked(json.dumps(ans.get("rows", [])), forbid, regexes)
        return CaseResult(
            cid,
            not hits,
            "; ".join(f"leaked '{x}'" for x in hits) or f"held ({ans['status']})",
            leak=bool(hits),
            extra={"sql": ans.get("sql")},
        )

    raise ValueError(f"unknown kind {kind}")


async def run(h: Harness, n: int | None = None, pace_s: float = 0.0) -> SuiteResult:
    suite = SuiteResult("rbac")
    for case in sample([c for c in load("rbac.yaml")["cases"] if c["kind"] not in SKIP_KINDS], n):
        try:
            suite.cases.append(await _case(h, case))
        except Exception as exc:  # a broken case must not hide the others
            suite.cases.append(
                CaseResult(case["id"], False, f"{type(exc).__name__}: {exc}", error=True)
            )
        if pace_s and case["kind"] in ("ask", "nl_sql"):
            await asyncio.sleep(pace_s)
    by_kind: dict[str, list[int]] = {}
    spec = {c["id"]: c["kind"] for c in load("rbac.yaml")["cases"]}
    for c in suite.cases:
        k = by_kind.setdefault(spec[c.id], [0, 0])
        k[0] += c.passed
        k[1] += 1
    suite.metrics = {
        "leaks": suite.leaks,
        "by_kind": {k: f"{p}/{t}" for k, (p, t) in by_kind.items()},
    }
    return suite
