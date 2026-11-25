"""Request + LLM telemetry.

* `record_usage` writes one llm_usage_logs row per LLM call or cache hit, scoped by RLS like
  any other write, and never stores prompts or answers. Error text is redacted.
* `telemetry_middleware` assigns a request id, binds it to structlog, and emits one access-log
  line per request with LLM totals (calls, tokens, cache hits).
Telemetry must never break a request: write failures are logged and swallowed.
"""

import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

import structlog
from fastapi import Request
from sqlalchemy import insert

from app.core.config import get_settings
from app.core.context import RequestContext, scoped_session
from app.models import LlmUsageLog

log = structlog.get_logger("telemetry")

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# order matters: whole tokens first, so the phone pattern can't split a key into pieces
_REDACTIONS = (
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[api-key]"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "[jwt]"),
    (re.compile(r"(?i)(bearer|api[_-]?key|password|secret)[=:\s]+\S+"), r"\1=[redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[phone]"),
)


def redact(text: str | None, max_len: int = 500) -> str | None:
    if text is None:
        return None
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text[:max_len]


@dataclass
class UsageRecord:
    route_type: str
    latency_ms: int
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit: bool = False
    status: str = "ok"  # ok | error
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        s = get_settings()
        return (
            self.prompt_tokens * s.llm_price_in_per_mtok
            + self.completion_tokens * s.llm_price_out_per_mtok
        ) / 1_000_000


@dataclass
class RequestUsage:
    records: list[UsageRecord] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "llm_calls": sum(1 for r in self.records if not r.cache_hit),
            "cache_hits": sum(1 for r in self.records if r.cache_hit),
            "tokens": sum(r.prompt_tokens + r.completion_tokens for r in self.records),
        }


_request_usage: ContextVar[RequestUsage | None] = ContextVar("request_usage", default=None)


def current_usage() -> RequestUsage | None:
    return _request_usage.get()


async def record_usage(ctx: RequestContext | None, rec: UsageRecord) -> None:
    rec.error = redact(rec.error)
    usage = _request_usage.get()
    if usage is not None:
        usage.records.append(rec)
    log.info(
        "llm_usage",
        route_type=rec.route_type,
        model=rec.model,
        latency_ms=rec.latency_ms,
        prompt_tokens=rec.prompt_tokens,
        completion_tokens=rec.completion_tokens,
        cache_hit=rec.cache_hit,
        status=rec.status,
    )
    if ctx is None:  # scripts / evals without a tenant scope: log line only
        return
    try:
        async with scoped_session(ctx) as session:
            # Core table insert: no RETURNING, because RETURNING is checked against the read
            # policy and non-staff roles may write telemetry rows but not read them back.
            await session.execute(
                insert(LlmUsageLog.__table__).values(
                    tenant_id=ctx.tenant_id,
                    event_id=ctx.event_id,
                    request_id=ctx.request_id,
                    user_id=ctx.user_id,
                    user_role=ctx.role,
                    route_type=rec.route_type,
                    model=rec.model,
                    latency_ms=rec.latency_ms,
                    prompt_tokens=rec.prompt_tokens,
                    completion_tokens=rec.completion_tokens,
                    cost_usd=rec.cost_usd,
                    cache_hit=rec.cache_hit,
                    status=rec.status,
                    error=rec.error,
                )
            )
    except Exception as exc:  # never fail the request because telemetry failed
        log.warning("llm_usage_write_failed", error=type(exc).__name__, detail=redact(str(exc)))


def new_request_id(incoming: str | None) -> str:
    if incoming and _REQUEST_ID_OK.match(incoming):
        return incoming
    return uuid.uuid4().hex


async def telemetry_middleware(request: Request, call_next):
    request_id = new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = request_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    usage = RequestUsage()
    token = _request_usage.set(usage)
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        ctx: RequestContext | None = getattr(request.state, "ctx", None)
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=status,
            latency_ms=round((time.perf_counter() - t0) * 1000),
            tenant_id=str(ctx.tenant_id) if ctx else None,
            event_id=str(ctx.event_id) if ctx else None,
            user_role=ctx.role if ctx else None,
            **usage.summary(),
        )
        _request_usage.reset(token)
