"""Shared plumbing for the eval suites.

* The API is called in-process (httpx ASGI transport), so every case goes through the real
  auth, RBAC, rate-limit and telemetry middleware exactly like the console does.
* Golden-set SQL (`vars`, `forbid_var`, labels) runs as the owner role: it describes what the
  data *is*, independent of what the code under test lets a persona see.
* Every API request carries an X-Request-ID `ev<run>-<suite>-<case>`, so the token and model
  numbers in the report come straight from llm_usage_logs for this run.
"""

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.core.context import RequestContext

GOLDEN = Path(__file__).parent / "golden"

PERSONAS = {
    "sara": "sara@northwind-health.example.com",
    "omar": "omar@northwind-health.example.com",
    "ali": "ali@northwind-health.example.com",
    "maya": "maya@medscan.example.com",
    "tom": "tom@cardiovue.example.com",
    "grace": "grace@apex-expo.example.com",
    "leo": "leo@apex-expo.example.com",
    "nina": "nina@cloudforge.example.com",
}

DECLINE = re.compile(
    r"i don't know|i do not know|couldn't find|could not find|can only help with|"
    r"can't help with|cannot help with|not able to|don't have (that|this|any)|no information",
    re.IGNORECASE,
)


def load(name: str) -> dict:
    return yaml.safe_load((GOLDEN / name).read_text(encoding="utf-8"))


@dataclass
class Login:
    persona: str
    email: str
    token: str
    ctx: RequestContext
    event_name: str


@dataclass
class Timing:
    suite: str
    case: str
    ms: float
    cached: bool
    route: str | None = None


@dataclass
class Harness:
    run_id: str
    client: httpx.AsyncClient
    owner: AsyncEngine
    timings: list[Timing] = field(default_factory=list)
    _logins: dict[tuple[str, str | None], Login] = field(default_factory=dict)

    @classmethod
    async def open(cls, app) -> "Harness":
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://eval", timeout=180
        )
        owner = create_async_engine(get_settings().migration_database_url)
        return cls(run_id=uuid.uuid4().hex[:6], client=client, owner=owner)

    async def close(self) -> None:
        await self.client.aclose()
        await self.owner.dispose()

    # --- auth ------------------------------------------------------------------------------

    async def login(self, persona: str, event: str | None = None) -> Login:
        key = (persona, event)
        if key in self._logins:
            return self._logins[key]
        email = PERSONAS.get(persona, persona)  # a persona key or a generated attendee's email
        body: dict[str, Any] = {"email": email}
        if event:
            rows = await self.rows(
                "SELECT event_id FROM auth_memberships(:e) m JOIN events ev ON ev.id = m.event_id "
                "WHERE ev.name LIKE :p",
                {"e": email, "p": event + "%"},
            )
            body["event_id"] = str(rows[0]["event_id"])
        r = await self.client.post("/auth/demo-login", json=body)
        r.raise_for_status()
        d = r.json()
        ctx = RequestContext(
            tenant_id=uuid.UUID(d["tenant_id"]),
            event_id=uuid.UUID(d["event_id"]),
            role=d["role"],
            user_id=uuid.UUID(d["user_id"]),
            exhibitor_id=uuid.UUID(d["exhibitor_id"]) if d["exhibitor_id"] else None,
        )
        login = Login(persona, email, d["access_token"], ctx, d["event_name"])
        self._logins[key] = login
        return login

    @staticmethod
    def forged(token: str) -> str:
        """Same signature, edited claims: role -> organizer, tenant -> zeros."""
        head, payload, sig = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims |= {"role": "organizer", "tid": str(uuid.UUID(int=0))}
        raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"{head}.{raw}.{sig}"

    # --- API -------------------------------------------------------------------------------

    async def call(
        self,
        suite: str,
        case: str,
        method: str,
        path: str,
        *,
        login: Login | None = None,
        body: Any = None,
        token: str | None = None,
        timed: bool = True,
    ) -> tuple[int, Any, float]:
        headers = {"X-Request-ID": f"ev{self.run_id}-{suite}-{case}"[:64]}
        tok = token if token is not None else (login.token if login else None)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        t0 = time.perf_counter()
        r = await self.client.request(method, path, json=body, headers=headers)
        ms = (time.perf_counter() - t0) * 1000
        try:
            data = r.json()
        except ValueError:
            data = r.text
        if timed and isinstance(data, dict) and r.status_code == 200:
            self.timings.append(
                Timing(suite, case, ms, bool(data.get("cached")), data.get("route"))
            )
        return r.status_code, data, ms

    # --- owner-side SQL --------------------------------------------------------------------

    async def rows(self, sql: str, params: dict | None = None) -> list[dict]:
        async with self.owner.connect() as c:
            return [dict(r) for r in (await c.execute(text(sql), params or {})).mappings().all()]

    async def resolve_vars(self, sql: str | None, login: Login) -> dict[str, str]:
        if not sql:
            return {}
        rows = await self.rows(sql, self._params(sql, login))
        if not rows:
            raise LookupError("vars query returned no rows")
        return {k: "" if v is None else str(v) for k, v in rows[0].items()}

    async def values(self, sql: str | None, login: Login) -> list[str]:
        """Pipe-separated values from the first column of a one-row query."""
        if not sql:
            return []
        rows = await self.rows(sql, self._params(sql, login))
        raw = next(iter(rows[0].values()), None) if rows else None
        return [v for v in (raw or "").split("|") if v.strip()]

    @staticmethod
    def _params(sql: str, login: Login) -> dict:
        p: dict[str, Any] = {}
        if ":event" in sql:
            p["event"] = login.ctx.event_id
        if ":tenant" in sql:
            p["tenant"] = login.ctx.tenant_id
        return p

    # --- telemetry for this run ------------------------------------------------------------

    async def usage(self, run_ids: list[str] | None = None) -> list[dict]:
        out: list[dict] = []
        for rid in run_ids or [self.run_id]:
            out += await self.rows(
                "SELECT request_id, route_type, model, latency_ms, prompt_tokens, "
                "completion_tokens, cache_hit, status, error FROM llm_usage_logs "
                "WHERE request_id LIKE :p",
                {"p": f"ev{rid}-%"},
            )
        return out


def fill(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def contains(haystack: str, needle: str) -> bool:
    return norm(needle) in norm(haystack)


def pct(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = (len(s) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)
