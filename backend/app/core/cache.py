"""Exact-match response cache in Redis, scoped so a cached answer can never cross a boundary.

Key = cache:{namespace}:{tenant}:{event}:v{event_version}:{scope}:{sha256(normalized query)}
  scope = role, plus exhibitor_id for exhibitors (their answers can include private material),
          plus user_id when the answer is personalized (e.g. "my bookmarks").
Invalidation: bump the event version (INCR, atomic). Old keys stop being read immediately and
expire on their own TTL, so there is no scan/delete.
"""

import hashlib
import json
import re
import unicodedata
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.context import RequestContext

_WS = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s?!.]+$")


def normalize_query(query: str) -> str:
    q = unicodedata.normalize("NFKC", query).casefold().strip()
    q = _WS.sub(" ", q)
    return _TRAILING.sub("", q)


def _version_key(ctx: RequestContext) -> str:
    return f"cache:ver:{ctx.tenant_id}:{ctx.event_id}"


def scope_part(ctx: RequestContext, personalized: bool) -> str:
    parts = [ctx.role]
    if ctx.exhibitor_id:
        parts.append(f"x{ctx.exhibitor_id}")
    if personalized:
        parts.append(f"u{ctx.user_id}")
    return ":".join(parts)


class ResponseCache:
    def __init__(self, redis: Redis, default_ttl_s: int | None = None) -> None:
        self.redis = redis
        self.default_ttl_s = default_ttl_s or get_settings().cache_default_ttl_s

    async def event_version(self, ctx: RequestContext) -> int:
        return int(await self.redis.get(_version_key(ctx)) or 0)

    async def key(
        self, ctx: RequestContext, namespace: str, query: str, *, personalized: bool = False
    ) -> str:
        digest = hashlib.sha256(normalize_query(query).encode()).hexdigest()[:32]
        version = await self.event_version(ctx)
        return (
            f"cache:{namespace}:{ctx.tenant_id}:{ctx.event_id}:v{version}:"
            f"{scope_part(ctx, personalized)}:{digest}"
        )

    async def get(
        self, ctx: RequestContext, namespace: str, query: str, *, personalized: bool = False
    ) -> Any | None:
        raw = await self.redis.get(await self.key(ctx, namespace, query, personalized=personalized))
        return json.loads(raw) if raw is not None else None

    async def set(
        self,
        ctx: RequestContext,
        namespace: str,
        query: str,
        value: Any,
        *,
        ttl_s: int | None = None,
        personalized: bool = False,
    ) -> None:
        key = await self.key(ctx, namespace, query, personalized=personalized)
        await self.redis.set(key, json.dumps(value, default=str), ex=ttl_s or self.default_ttl_s)

    async def invalidate_event(self, ctx: RequestContext) -> int:
        """Call whenever event content changes (webhook, schedule edit, re-index)."""
        return int(await self.redis.incr(_version_key(ctx)))
