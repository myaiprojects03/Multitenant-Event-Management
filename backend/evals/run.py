"""Run the evaluation suites and write a markdown report.

    cd backend
    .\\.venv\\Scripts\\python -m evals.run                 # everything (uses Gemini quota)
    .\\.venv\\Scripts\\python -m evals.run --sample 5      # a few cases per suite
    .\\.venv\\Scripts\\python -m evals.run --no-llm        # deterministic parts only (CI)
    .\\.venv\\Scripts\\python -m evals.run --suites rag,nl_sql

Needs Postgres + Redis up, migrated and seeded (reseed first if the date changed).
"""

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# evals call the API far faster than a person would: lift the per-user limits for this process
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE_ORGANIZER", "100000")
os.environ.setdefault("RATE_LIMIT_PER_DAY", "100000")

from app.core.cache import ResponseCache  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.context import RequestContext  # noqa: E402
from app.core.db import dispose_engine  # noqa: E402
from app.core.redis import close_redis, get_redis  # noqa: E402
from app.main import app  # noqa: E402
from evals import report  # noqa: E402
from evals import results as results_mod  # noqa: E402
from evals.harness import Harness, Timing  # noqa: E402
from evals.results import CaseResult, SuiteResult  # noqa: E402
from evals.suites import matching, nl_sql, rag, rbac, sql_safety, workflows  # noqa: E402

SUITES = {
    "workflows": workflows,
    "matching": matching,
    "sql_safety": sql_safety,
    "rbac": rbac,
    "rag": rag,
    "nl_sql": nl_sql,
}
LLM_ONLY = {"rag", "nl_sql"}
REPORTS = Path(__file__).parent / "reports"


def _commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        return sha + (" + uncommitted changes" if dirty else "")
    except Exception:
        return "unknown"


async def _fresh_cache(h: Harness) -> None:
    """Bump every event's cache version so the first pass is uncached."""
    cache = ResponseCache(get_redis())
    for e in await h.rows("SELECT id, tenant_id FROM events"):
        await cache.invalidate_event(RequestContext.system(e["tenant_id"], e["id"]))


def _load(path: Path) -> tuple[dict, dict[str, SuiteResult]]:
    prev = json.loads(path.read_text(encoding="utf-8"))
    suites = {
        name: SuiteResult(name, [CaseResult(**c) for c in s["cases"]], s["metrics"])
        for name, s in prev["suites"].items()
    }
    return prev, suites


def report_only(args) -> int:
    """Rebuild latest.md from latest.json (after editing the report code or adding notes)."""
    path = Path(args.out) if args.out else REPORTS / "latest.md"
    prev, suites = _load(path.with_suffix(".json"))
    if args.note:
        prev["info"]["notes"] = prev["info"].get("notes", []) + [args.note]
        path.with_suffix(".json").write_text(json.dumps(prev, indent=1, default=str), "utf-8")
    report.write(path, suites, prev["perf"], prev["info"])
    print(f"report: {path}")
    return 0


async def main(args) -> int:
    names = [n.strip() for n in args.suites.split(",")] if args.suites else list(SUITES)
    if args.no_llm:
        names = [n for n in names if n not in LLM_ONLY]
        rbac.SKIP_KINDS = {"ask", "nl_sql"}
        sql_safety.SKIP_PROMPTS = True
    prev, previous = None, {}
    rerun: dict[str, set[str]] = {}
    if args.merge:  # run whole suites again and replace just those in the full report
        prev, previous = _load(REPORTS / "latest.json")
    if args.rerun_errors:
        prev, previous = _load(REPORTS / "latest.json")
        for name, suite in previous.items():
            ids = {c.id.split(":", 1)[-1] for c in suite.cases if c.error}
            if ids:
                rerun[name] = ids
        names = [n for n in SUITES if n in rerun]
        print(f"re-running infra errors: { {k: len(v) for k, v in rerun.items()} }")
    h = await Harness.open(app)
    results = {}
    try:
        await _fresh_cache(h)
        for name in names:
            t0 = time.perf_counter()
            print(f"[{name}] running...", flush=True)
            results_mod.ONLY = rerun.get(name)
            try:
                results[name] = await SUITES[name].run(h, args.sample, args.pace)
            except Exception as exc:  # keep going: a crashed suite is reported, not hidden
                results[name] = SuiteResult(
                    name,
                    [
                        CaseResult(
                            "suite", False, f"crashed: {type(exc).__name__}: {exc}", error=True
                        )
                    ],
                )
            r = results[name]
            print(
                f"[{name}] {r.passed}/{len(r.cases)} passed, {r.leaks} leaks, {r.errors} infra "
                f"errors ({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )
        run_ids = [h.run_id]
        timings = [dataclasses.asdict(t) for t in h.timings]
        if prev:  # merge the re-run cases into the previous full report
            run_ids = prev["info"].get("run_ids", [prev["info"]["run_id"]]) + [h.run_id]
            old_timings = prev.get("timings", [])
            replaced: set[str] = set()
            if args.merge:
                replaced = set(results)
                previous.update(results)
                old_timings = [
                    t for t in old_timings if t["suite"].removesuffix("-cached") not in replaced
                ]
            else:
                redone = {k: {c.id: c for c in v.cases} for k, v in results.items()}
                for name, suite in previous.items():
                    new = redone.get(name, {})
                    suite.cases = [new.get(c.id, c) for c in suite.cases]
                    if hasattr(SUITES[name], "metrics"):
                        suite.metrics = {**suite.metrics, **SUITES[name].metrics(suite)}
            results = previous
            timings = old_timings + timings
        usage = await h.usage(run_ids)
        if prev and args.merge:  # drop the earlier runs' calls for the suites run again
            stale = tuple(f"ev{rid}-{name}" for rid in run_ids[:-1] for name in replaced)
            usage = [u for u in usage if not u["request_id"].startswith(stale)]
        perf = {
            "latency": report.latency([Timing(**t) for t in timings]),
            "tokens": report.tokens(usage),
        }
    finally:
        await h.close()
        await dispose_engine()
        await close_redis()

    s = get_settings()
    info = {
        **(prev["info"] if prev else {}),
        "run_ids": run_ids,
        "reruns": (prev["info"].get("reruns", 0) + (not args.merge)) if prev else 0,
        "merged_suites": sorted(
            set(prev["info"].get("merged_suites", [])) | (set(names) if args.merge else set())
        )
        if prev
        else [],
        "run_id": h.run_id if not prev else prev["info"]["run_id"],
        "date": f"{dt.date.today():%Y-%m-%d}",
        "commit": _commit(),
        "sample": args.sample,
        "no_llm": args.no_llm,
        "fast": s.gemini_model_fast,
        "smart": s.gemini_model_smart,
    }
    out = (
        Path(args.out) if args.out else REPORTS / ("latest.md" if not args.sample else "sample.md")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    report.write(out, results, perf, info)
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                "info": info,
                "perf": perf,
                "timings": timings,
                "suites": {k: dataclasses.asdict(v) for k, v in results.items()},
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"report: {out}")
    leaks = sum(r.leaks for r in results.values())
    failed = sum(len(r.cases) - r.passed - r.errors for r in results.values())
    if leaks or (args.strict and failed):
        print(f"FAILED: {leaks} leaks, {failed} failing cases")
        return 1
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--suites", help="comma-separated: " + ",".join(SUITES))
    p.add_argument("--sample", type=int, help="cases per suite (spread across the file)")
    p.add_argument("--pace", type=float, default=1.0, help="seconds between LLM cases")
    p.add_argument("--no-llm", action="store_true", help="skip everything that calls Gemini")
    p.add_argument("--strict", action="store_true", help="exit 1 on any failing case (CI)")
    p.add_argument("--report-only", action="store_true", help="rebuild the md from latest.json")
    p.add_argument("--note", help="with --report-only: add a note to the report")
    p.add_argument(
        "--merge",
        action="store_true",
        help="run the --suites again and replace only those in latest.json/md",
    )
    p.add_argument(
        "--rerun-errors",
        action="store_true",
        help="re-run only the cases of latest.json that hit infra errors and merge them in",
    )
    p.add_argument("--out", help="report path (default evals/reports/latest.md)")
    a = p.parse_args()
    sys.exit(report_only(a) if a.report_only else asyncio.run(main(a)))
