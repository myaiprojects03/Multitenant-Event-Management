"""BI copilot execution accuracy (golden/nl_sql.yaml). Target: >= 80%."""

import asyncio
import datetime as dt
from collections import Counter

from app.modules.analytics.executor import run_guarded
from app.modules.analytics.guard import guard_sql
from evals.harness import Harness, load, pct
from evals.results import CaseResult, SuiteResult, sample


def _norm(v):
    """Comparable form: numbers rounded to 3 significant-ish places, text lowercased."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return round(float(v), 1) if abs(v) < 1000 else round(float(v))
    s = str(v).strip()
    try:
        if len(s) >= 10 and s[4] == "-":  # ISO date / timestamp -> the date part
            return dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    try:
        return _norm(float(s))
    except ValueError:
        return s.lower()


def _close(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(0.011 * max(abs(a), abs(b)), 0.06)
    return a == b


def _same(seq_a: list, seq_b: list, ordered: bool) -> bool:
    if len(seq_a) != len(seq_b):
        return False
    if ordered:
        return all(_close(x, y) for x, y in zip(seq_a, seq_b, strict=True))
    if all(not isinstance(x, float) for x in seq_a + seq_b):
        return Counter(map(str, seq_a)) == Counter(map(str, seq_b))
    return _same(sorted(seq_a, key=str), sorted(seq_b, key=str), True)


def compare(ref_cols, ref_rows, cols, rows, mode: str) -> tuple[bool, str]:
    if mode == "first":
        if not rows or not ref_rows:
            return False, "no rows" if not rows else "reference has no rows"
        ref_rows, rows = ref_rows[:1], rows[:1]
    ordered = mode == "ordered"
    if mode != "first" and len(rows) != len(ref_rows):
        return False, f"{len(rows)} rows, expected {len(ref_rows)}"
    answer_cols = {c: [_norm(r.get(c)) for r in rows] for c in cols}
    for rc in ref_cols:
        want = [_norm(r.get(rc)) for r in ref_rows]
        if not any(_same(want, got, ordered) for got in answer_cols.values()):
            return False, f"no column matches reference '{rc}' = {want[:5]}"
    return True, "match"


def metrics(suite: SuiteResult) -> dict:
    answered = [c for c in suite.cases if not c.error]
    return {
        "accuracy": pct(suite.passed, len(suite.cases)),
        "accuracy_excluding_infra_errors": pct(sum(c.passed for c in answered), len(answered)),
        "repaired_once": sum(bool(c.extra.get("repaired")) for c in suite.cases),
    }


async def run(h: Harness, n: int | None = None, pace_s: float = 0.0) -> SuiteResult:
    spec = load("nl_sql.yaml")
    d = spec.get("defaults", {})
    suite = SuiteResult("nl_sql")
    for case in sample(spec["cases"], n):
        case = {**d, **case}
        login = await h.login(case["as"], case.get("event"))
        ref = await run_guarded(login.ctx, guard_sql(case["ref"]))
        status, ans, ms = await h.call(
            "nl_sql",
            case["id"],
            "POST",
            "/analytics/nl-sql",
            login=login,
            body={"question": case["q"]},
        )
        if status != 200:
            suite.cases.append(CaseResult(case["id"], False, f"HTTP {status}", error=status >= 500))
            continue
        extra = {"q": case["q"], "sql": ans.get("sql"), "ms": round(ms)}
        if ans["status"] != "ok":
            suite.cases.append(
                CaseResult(
                    case["id"],
                    False,
                    f"{ans['status']}: {ans.get('error')}",
                    error="unavailable" in (ans.get("error") or ""),
                    extra=extra,
                )
            )
            continue
        extra["repaired"] = bool(ans.get("repaired"))
        ok, why = compare(
            ref.columns, ref.rows, ans["columns"], ans["rows"], case.get("match", "rows")
        )
        suite.cases.append(CaseResult(case["id"], ok, why, extra=extra))
        if pace_s:
            await asyncio.sleep(pace_s)
    suite.metrics = metrics(suite)
    return suite
