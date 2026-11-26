"""Event assistant: correctness, grounding and citation validity (golden/rag.yaml)."""

import asyncio

from evals.harness import DECLINE, Harness, contains, fill, load, pct
from evals.results import CaseResult, SuiteResult, sample
from evals.suites.common import check_citations

BUSY = "The assistant is busy right now"


def _judge(case: dict, answer: dict, values: dict[str, str]) -> list[str]:
    exp = case.get("expect", {})
    text_ = answer.get("answer", "")
    route = answer.get("route")
    problems: list[str] = []
    if exp.get("unknown"):
        if route not in ("restricted", "out_of_scope") and not DECLINE.search(text_):
            problems.append("should have declined")
        return problems
    if DECLINE.search(text_) or route in ("restricted", "out_of_scope"):
        problems.append(f"declined ({route})")
    if exp.get("route") and route not in exp["route"]:
        problems.append(f"route {route} not in {exp['route']}")
    any_ = [fill(s, values) for s in exp.get("any", [])]
    if "any_of_var" in exp:
        any_ += [v for v in values.get(exp["any_of_var"], "").split("|") if v]
    if any_ and not any(contains(text_, s) for s in any_):
        shown = ", ".join(any_[:4]) + (" ..." if len(any_) > 4 else "")
        problems.append(f"none of [{shown}]")
    for s in exp.get("all", []):
        if not contains(text_, fill(s, values)):
            problems.append(f"missing '{fill(s, values)}'")
    # tool answers (route "action") are database rows, not text synthesis: nothing to cite
    if exp.get("cite") and route != "action" and not answer.get("citations"):
        problems.append("no citation")
    return problems


def metrics(suite: SuiteResult) -> dict:
    answered = [c for c in suite.cases if not c.error]
    synth = [c for c in answered if c.extra.get("synthesized")]
    return {
        "correct": pct(suite.passed, len(suite.cases)),
        "correct_excluding_infra_errors": pct(sum(c.passed for c in answered), len(answered)),
        "citation_validity": pct(
            sum(c.extra.get("valid_citations", 0) for c in answered),
            sum(c.extra.get("citations", 0) for c in answered),
        ),
        "grounded": pct(sum(c.extra.get("citations", 0) > 0 for c in synth), len(synth)),
    }


async def run(h: Harness, n: int | None = None, pace_s: float = 0.0) -> SuiteResult:
    spec = load("rag.yaml")
    defaults = spec.get("defaults", {})
    suite = SuiteResult("rag")
    replay: list[tuple[dict, object, str]] = []

    for case in sample(spec["cases"], n):
        case = {**defaults, **case}
        login = await h.login(case["as"], case.get("event"))
        try:
            values = await h.resolve_vars(case.get("vars"), login)
        except LookupError as exc:
            suite.cases.append(CaseResult(case["id"], False, str(exc), error=True))
            continue
        # pipe lists from a vars column (any_of_var) stay usable as {placeholders} too
        q = fill(case["q"], values)
        status, ans, ms = await h.call(
            "rag", case["id"], "POST", "/assistant/query", login=login, body={"question": q}
        )
        if status != 200 or not isinstance(ans, dict):
            suite.cases.append(CaseResult(case["id"], False, f"HTTP {status}", error=True))
            continue
        if ans["answer"].startswith(BUSY):
            suite.cases.append(CaseResult(case["id"], False, "LLM unavailable", error=True))
            continue
        problems = _judge(case, ans, values)
        valid, cite_problems = await check_citations(h, ans, login)
        leak = any("another event" in p or "_only" in p or "private" in p for p in cite_problems)
        synthesized = not case.get("expect", {}).get("unknown") and ans.get("route") == "general"
        suite.cases.append(
            CaseResult(
                case["id"],
                not problems and not cite_problems,
                "; ".join(problems + cite_problems),
                leak=leak,
                extra={
                    "q": q,
                    "route": ans.get("route"),
                    "ms": round(ms),
                    "answer": ans["answer"][:300],
                    "citations": len(ans.get("citations") or []),
                    "valid_citations": valid,
                    "synthesized": synthesized,
                },
            )
        )
        replay.append((case, login, q))
        if pace_s:
            await asyncio.sleep(pace_s)

    # the same questions again: these should now come from the response cache
    hits = 0
    for case, login, q in replay[:10]:
        status, ans, _ = await h.call(
            "rag-cached", case["id"], "POST", "/assistant/query", login=login, body={"question": q}
        )
        hits += bool(status == 200 and ans.get("cached"))
    suite.metrics = metrics(suite)
    suite.metrics["cache_replay_hits"] = f"{hits}/{min(len(replay), 10)}"
    return suite
