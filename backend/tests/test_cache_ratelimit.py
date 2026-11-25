"""Cache scoping/invalidation and rate limits against the real Redis (db 15)."""

import uuid

import pytest
from redis.asyncio import Redis

from app.core.cache import ResponseCache, normalize_query
from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.ratelimit import check_rate_limit


async def _redis_ready() -> bool:
    r = Redis.from_url(get_settings().redis_url)
    try:
        return bool(await r.ping())
    except Exception:
        return False
    finally:
        await r.aclose()


@pytest.fixture
async def redis():
    url = get_settings().redis_url.rsplit("/", 1)[0] + "/15"
    r = Redis.from_url(url, decode_responses=True)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not running")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


T1, T2, E1, E2 = (uuid.uuid4() for _ in range(4))
X1, X2 = uuid.uuid4(), uuid.uuid4()


def ctx(role="attendee", tenant=T1, event=E1, user=None, exhibitor=None) -> RequestContext:
    if role == "exhibitor" and exhibitor is None:
        exhibitor = X1
    return RequestContext(
        tenant_id=tenant, event_id=event, role=role, user_id=user or uuid.uuid4(),
        exhibitor_id=exhibitor,
    )  # fmt: skip


def test_normalize_query():
    assert normalize_query("  Where is   the WIFI?? ") == normalize_query("where is the wifi")


async def test_cache_hit_on_normalized_query(redis):
    c, a = ResponseCache(redis), ctx()
    await c.set(a, "faq", "Where is the Wi-Fi?", {"answer": "HTS-Guest"})
    assert await c.get(ctx(), "faq", "  where is the wi-fi ") == {"answer": "HTS-Guest"}


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(dict(role="organizer"), id="different-role"),
        pytest.param(dict(tenant=T2), id="different-tenant"),
        pytest.param(dict(event=E2), id="different-event"),
    ],
)
async def test_cache_never_crosses_scope(redis, other):
    c = ResponseCache(redis)
    await c.set(ctx(), "faq", "q", {"answer": "x"})
    assert await c.get(ctx(**other), "faq", "q") is None


async def test_exhibitor_answers_are_per_exhibitor(redis):
    c = ResponseCache(redis)
    await c.set(ctx("exhibitor", exhibitor=X1), "assistant", "our discount?", {"a": "15%"})
    assert await c.get(ctx("exhibitor", exhibitor=X2), "assistant", "our discount?") is None
    assert await c.get(ctx("exhibitor", exhibitor=X1), "assistant", "our discount?") == {"a": "15%"}


async def test_personalized_answers_are_per_user(redis):
    c, u1, u2 = ResponseCache(redis), uuid.uuid4(), uuid.uuid4()
    await c.set(ctx(user=u1), "assistant", "my bookmarks", {"a": 1}, personalized=True)
    assert await c.get(ctx(user=u2), "assistant", "my bookmarks", personalized=True) is None
    assert await c.get(ctx(user=u1), "assistant", "my bookmarks", personalized=True) == {"a": 1}


async def test_invalidate_event_drops_only_that_event(redis):
    c = ResponseCache(redis)
    await c.set(ctx(event=E1), "faq", "q", {"a": 1})
    await c.set(ctx(event=E2), "faq", "q", {"a": 2})
    assert await c.invalidate_event(ctx(event=E1)) == 1
    assert await c.get(ctx(event=E1), "faq", "q") is None
    assert await c.get(ctx(event=E2), "faq", "q") == {"a": 2}


async def test_cache_ttl_is_set(redis):
    c = ResponseCache(redis)
    await c.set(ctx(), "faq", "q", {"a": 1}, ttl_s=30)
    key = await c.key(ctx(), "faq", "q")
    assert 0 < await redis.ttl(key) <= 30


@pytest.fixture
def small_limits(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_per_minute", 3)
    monkeypatch.setattr(s, "rate_limit_per_minute_organizer", 5)
    monkeypatch.setattr(s, "rate_limit_per_day", 7)


async def test_rate_limit_per_minute(redis, small_limits):
    me, now = ctx(), 1_000_020.0
    results = [await check_rate_limit(redis, me, now=now) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after_s == 60 - int(now % 60)
    # another user is unaffected; the next minute resets
    assert (await check_rate_limit(redis, ctx(), now=now)).allowed
    assert (await check_rate_limit(redis, me, now=now + 60)).allowed


async def test_rate_limit_organizer_higher_and_daily_quota(redis, small_limits):
    org, now = ctx("organizer"), 2_000_000.0
    assert all([(await check_rate_limit(redis, org, now=now)).allowed for _ in range(5)])
    assert not (await check_rate_limit(redis, org, now=now)).allowed
    # daily quota (7) spans minutes
    user = ctx()
    allowed = [(await check_rate_limit(redis, user, now=now + 60 * i)).allowed for i in range(8)]
    assert allowed == [True] * 7 + [False]
