"""LLM gateway behaviour with a scripted fake Gemini client (no network)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from app.core.config import Settings
from app.core.llm import LLMError, LLMGateway, LLMUnavailableError


class Answer(BaseModel):
    intent: str
    confidence: float


def ok(text='{"intent": "lookup", "confidence": 0.9}', prompt=10, out=5, thoughts=3):
    return SimpleNamespace(
        text=text,
        parsed=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts
        ),
    )


def server_error(code=503):
    return genai_errors.ServerError(
        code, {"error": {"code": code, "message": "busy", "status": "UNAVAILABLE"}}
    )


def client_error(code=400):
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": "bad", "status": "INVALID_ARGUMENT"}}
    )


class FakeModels:
    def __init__(self, script):
        self.script = list(script)  # items: response | exception | ("sleep", seconds)
        self.calls: list[str] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        step = self.script.pop(0)
        if isinstance(step, tuple) and step[0] == "sleep":
            await asyncio.sleep(step[1])
            return ok()
        if isinstance(step, Exception):
            raise step
        return step


def gateway(script, **overrides):
    models = FakeModels(script)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    settings = Settings(
        gemini_api_key="test",
        gemini_model_fast="primary",
        gemini_fast_fallbacks="backup",
        llm_attempts_per_model=2,
        llm_backoff_base_s=0.5,
        llm_timeout_s=overrides.pop("timeout", 5.0),
        **overrides,
    )
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    return LLMGateway(client=client, settings=settings, sleep=fake_sleep), models, sleeps


async def call(gw, **kw):
    return await gw.generate("q", ctx=None, route_type="test", schema=Answer, **kw)


async def test_structured_output_and_token_counting():
    gw, models, _ = gateway([ok()])
    r = await call(gw)
    assert isinstance(r.parsed, Answer) and r.parsed.intent == "lookup"
    assert (r.prompt_tokens, r.completion_tokens) == (10, 8)  # completion includes thinking
    assert r.model == "primary" and r.attempts == 1 and models.calls == ["primary"]


async def test_retries_same_model_on_503_with_backoff():
    gw, models, sleeps = gateway([server_error(), ok()])
    r = await call(gw)
    assert models.calls == ["primary", "primary"] and r.model == "primary" and r.attempts == 2
    assert len(sleeps) == 1 and 0.5 <= sleeps[0] <= 0.75


async def test_falls_back_to_next_model_after_retries():
    gw, models, _ = gateway([server_error(), server_error(429), ok()])
    r = await call(gw)
    assert models.calls == ["primary", "primary", "backup"]
    assert r.model == "backup" and r.attempts == 3


async def test_timeout_is_retryable():
    gw, models, _ = gateway([("sleep", 1.0), ("sleep", 1.0), ok()], timeout=0.05)
    r = await call(gw)
    assert r.model == "backup" and models.calls == ["primary", "primary", "backup"]


async def test_network_error_is_retryable():
    gw, _, _ = gateway([httpx.ConnectError("reset"), ok()])
    assert (await call(gw)).attempts == 2


async def test_bad_request_fails_fast():
    gw, models, _ = gateway([client_error(400), ok()])
    with pytest.raises(LLMError) as exc:
        await call(gw)
    assert not isinstance(exc.value, LLMUnavailableError)
    assert models.calls == ["primary"]


async def test_invalid_structured_output_is_retried():
    gw, models, _ = gateway([ok(text="not json"), ok(text='{"intent": 1}'), ok()])
    r = await call(gw)
    assert r.parsed.intent == "lookup" and len(models.calls) == 3


async def test_all_models_failing_raises_unavailable_with_reasons():
    gw, _, _ = gateway([server_error()] * 4)
    with pytest.raises(LLMUnavailableError, match="primary:http_503.*backup:http_503"):
        await call(gw)


async def test_free_text_without_schema():
    gw, _, _ = gateway([ok(text="Room 101")])
    r = await gw.generate("q", ctx=None, route_type="test")
    assert r.text == "Room 101" and r.parsed is None


def test_model_chain_dedupes_and_orders():
    s = Settings(gemini_model_smart="a", gemini_smart_fallbacks=" b, a ,c,")
    assert s.model_chain("smart") == ["a", "b", "c"]
