"""Health endpoint tests with fake DB/Redis, so they run without Docker."""

from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.redis import get_redis
from app.main import app


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeSession:
    def __init__(self, has_vector=True, fail=False):
        self.has_vector, self.fail = has_vector, fail

    async def execute(self, stmt):
        if self.fail:
            raise ConnectionRefusedError
        return _Result(1 if self.has_vector or "pg_extension" not in str(stmt) else None)


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail

    async def ping(self):
        if self.fail:
            raise ConnectionRefusedError
        return True


def _client(session, redis) -> TestClient:
    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_redis] = lambda: redis
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_health_ok():
    r = _client(FakeSession(), FakeRedis()).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_db_down():
    r = _client(FakeSession(fail=True), FakeRedis()).get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["db"] == {"status": "error", "detail": "ConnectionRefusedError"}


def test_health_missing_pgvector():
    r = _client(FakeSession(has_vector=False), FakeRedis()).get("/health")
    assert r.status_code == 503
    assert "pgvector" in r.json()["checks"]["db"]["detail"]


def test_health_redis_down():
    r = _client(FakeSession(), FakeRedis(fail=True)).get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"]["status"] == "error"
