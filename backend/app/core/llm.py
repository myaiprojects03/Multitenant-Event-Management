"""LLM gateway: the only place the app talks to Gemini.

* Routing: tier "fast" (classification, routing, phrasing) or "smart" (synthesis, SQL).
  Each tier is a model chain from config: primary first, then fallbacks.
* Reliability: every call has a hard timeout. Timeouts, 429 and 5xx are retried with
  exponential backoff + jitter, then the next model in the chain is tried. Other 4xx errors
  (bad request, auth) fail immediately because retrying cannot help.
* Cooldown: a model that exhausts its attempts on overload (503/504/timeout), or answers 429
  (quota; not retried), is skipped for `llm_busy_cooldown_s`, so later calls go straight to the
  fallback instead of paying the same retries again. Cooling models are still tried once, last,
  if every ready model fails.
* Structured output: pass a Pydantic model as `schema`; the response is validated against it
  and an invalid response counts as a retryable failure.
* Telemetry: one llm_usage_logs row per gateway call (model actually used, tokens, latency).
"""

import asyncio
import random
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Generic, Literal, TypeVar

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.context import RequestContext
from app.core.telemetry import UsageRecord, record_usage

log = structlog.get_logger("llm")

Tier = Literal["fast", "smart"]
ThinkingLevel = Literal["minimal", "low", "medium", "high"]
T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
OVERLOAD = frozenset({"http_429", "http_503", "http_504", "timeout"})


class LLMError(Exception):
    """Non-retryable failure (bad request, auth, quota misconfiguration...)."""


class LLMUnavailableError(LLMError):
    """Every model in the chain failed after retries."""


@dataclass
class LLMResponse(Generic[T]):
    text: str
    parsed: T | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    attempts: int


class _Retryable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _usage_tokens(resp: Any) -> tuple[int, int]:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return 0, 0
    prompt = u.prompt_token_count or 0
    completion = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
    return prompt, completion


class LLMGateway:
    def __init__(
        self,
        client: Any | None = None,
        settings: Settings | None = None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._client_loop = None  # serverless: the loop our own client was built on
        self._owns_client = client is None
        self._sleep = sleep
        self._clock = clock
        self._busy_until: dict[str, float] = {}  # model -> monotonic time it may be tried again
        self._shared_cooling: list[tuple[str, float]] = []  # cooldowns not yet saved to Redis

    def _plan(self, chain: list[str]) -> list[tuple[str, int]]:
        """(model, attempts): ready models first with full retries, then models still cooling
        down with a single last-resort attempt (they may have recovered)."""
        now = self._clock()
        n = self.settings.llm_attempts_per_model
        ready = [m for m in chain if self._busy_until.get(m, 0.0) <= now]
        return [(m, n) for m in ready] + [(m, 1) for m in chain if m not in ready]

    def _cool(self, model: str, reason: str = "") -> None:
        seconds = self.settings.llm_busy_cooldown_s
        if reason == "http_429":  # a spent (daily) quota won't come back in two minutes
            seconds = max(seconds, self.settings.llm_quota_cooldown_s)
        if seconds > 0:
            self._busy_until[model] = self._clock() + seconds
            self._shared_cooling.append((model, seconds))
            log.warning("llm_model_cooldown", model=model, seconds=seconds)

    # Serverless: each instance would otherwise learn about a busy model the slow way (a 30 s
    # timeout or a 429 per instance), so cooldowns are also kept in Redis with a TTL.
    _SHARED = "llm:cooling:"

    async def _load_shared(self, chain: list[str]) -> None:
        if not self.settings.serverless:
            return
        try:
            from app.core.redis import get_redis

            redis = get_redis()
            for model in chain:
                ttl = await redis.ttl(self._SHARED + model)
                if ttl and ttl > 0:
                    until = self._clock() + ttl
                    self._busy_until[model] = max(self._busy_until.get(model, 0.0), until)
        except Exception:  # Redis trouble must never block an LLM call
            log.warning("llm_shared_cooldown_unavailable")

    async def _save_shared(self) -> None:
        pending, self._shared_cooling = self._shared_cooling, []
        if not (pending and self.settings.serverless):
            return
        try:
            from app.core.redis import get_redis

            redis = get_redis()
            for model, seconds in pending:
                await redis.set(self._SHARED + model, "1", ex=max(int(seconds), 1))
        except Exception:
            log.warning("llm_shared_cooldown_unavailable")

    @property
    def client(self) -> Any:
        from app.core.loops import current_loop

        if (
            self._owns_client
            and self.settings.serverless
            and self._client_loop is not current_loop()
        ):
            self._client = None  # its HTTP connections belong to a finished event loop
        if self._client is None:
            key = self.settings.gemini_api_key.get_secret_value()
            if not key:
                raise LLMError("GEMINI_API_KEY is not set")
            self._client = genai.Client(
                api_key=key,
                # SDK-level timeout as a second line of defence behind asyncio.wait_for
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_s * 1000)),
            )
            self._client_loop = current_loop()
        return self._client

    def _config(
        self,
        schema: type[BaseModel] | None,
        system: str | None,
        temperature: float,
        thinking_level: ThinkingLevel | None,
        max_output_tokens: int | None,
    ) -> types.GenerateContentConfig:
        cfg: dict[str, Any] = {"temperature": temperature}
        if system:
            cfg["system_instruction"] = system
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = schema
        if thinking_level:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        if max_output_tokens:
            cfg["max_output_tokens"] = max_output_tokens
        return types.GenerateContentConfig(**cfg)

    async def _attempt(self, model: str, contents: Any, config, schema):
        try:
            resp = await asyncio.wait_for(
                self.client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                ),
                timeout=self.settings.llm_timeout_s,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise _Retryable("timeout") from exc
        except httpx.TransportError as exc:  # connection reset, DNS, etc.
            raise _Retryable("network") from exc
        except genai_errors.APIError as exc:
            if exc.code in RETRYABLE_STATUS:
                raise _Retryable(f"http_{exc.code}") from exc
            raise LLMError(f"{model}: HTTP {exc.code} {exc.status}") from exc

        text = resp.text or ""
        parsed = None
        if schema is not None:
            parsed = resp.parsed if isinstance(resp.parsed, schema) else None
            if parsed is None:
                try:
                    parsed = schema.model_validate_json(text)
                except ValidationError as exc:
                    raise _Retryable("invalid_output") from exc
        return resp, text, parsed

    async def generate(
        self,
        contents: Any,
        *,
        ctx: RequestContext | None,
        route_type: str,
        tier: Tier = "fast",
        schema: type[T] | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        thinking_level: ThinkingLevel | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse[T]:
        s = self.settings
        config = self._config(schema, system, temperature, thinking_level, max_output_tokens)
        chain = s.model_chain(tier)
        t0 = time.perf_counter()
        failures: list[str] = []
        await self._load_shared(chain)
        try:
            return await self._generate(
                contents, chain, config, schema, ctx, route_type, t0, failures
            )
        finally:
            await self._save_shared()

    async def _generate(self, contents, chain, config, schema, ctx, route_type, t0, failures):
        s = self.settings
        attempts = 0
        for model, tries in self._plan(chain):
            reasons: list[str] = []
            for i in range(tries):
                attempts += 1
                try:
                    resp, text, parsed = await self._attempt(model, contents, config, schema)
                except _Retryable as exc:
                    failures.append(f"{model}:{exc.reason}")
                    reasons.append(exc.reason)
                    log.warning("llm_retry", model=model, reason=exc.reason, attempt=attempts)
                    if exc.reason == "http_429":  # quota/rate limit: retrying now won't help
                        self._cool(model, "http_429")
                        break
                    if i + 1 < tries:
                        delay = s.llm_backoff_base_s * 2**i
                        await self._sleep(delay + random.uniform(0, delay / 2))
                    elif all(r in OVERLOAD for r in reasons):
                        self._cool(model)
                    continue
                except LLMError as exc:
                    await self._record(ctx, route_type, model, t0, status="error", error=str(exc))
                    raise
                prompt_tokens, completion_tokens = _usage_tokens(resp)
                latency_ms = round((time.perf_counter() - t0) * 1000)
                await record_usage(
                    ctx,
                    UsageRecord(
                        route_type=route_type,
                        model=model,
                        latency_ms=latency_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        error="; ".join(failures) or None,  # earlier failures, if we fell back
                    ),
                )
                return LLMResponse(
                    text=text,
                    parsed=parsed,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    attempts=attempts,
                )

        error = "all models failed: " + "; ".join(failures)
        await self._record(ctx, route_type, None, t0, status="error", error=error)
        raise LLMUnavailableError(error)

    async def _record(self, ctx, route_type, model, t0, *, status, error) -> None:
        await record_usage(
            ctx,
            UsageRecord(
                route_type=route_type,
                model=model,
                latency_ms=round((time.perf_counter() - t0) * 1000),
                status=status,
                error=error,
            ),
        )


@lru_cache
def get_llm() -> LLMGateway:
    return LLMGateway()
