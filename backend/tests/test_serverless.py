"""Serverless mode (Vercel): queued jobs run inline, after their delay, and a failing job
never fails the request that queued it."""

import pytest

from app.core import jobs
from app.core.config import Settings
from app.workers import main as worker


@pytest.fixture
def serverless(monkeypatch):
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(_env_file=None, serverless=True))
    calls = []

    async def ok_job(ctx, *args):
        calls.append(args)

    async def bad_job(ctx, *args):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker.WorkerSettings, "functions", [ok_job, bad_job])
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(jobs.asyncio, "sleep", fake_sleep)
    return calls, sleeps


async def test_enqueue_runs_the_job_inline_after_its_delay(serverless):
    calls, sleeps = serverless
    assert await jobs.enqueue("ok_job", "t", "e", defer_s=2) is None
    assert calls == [("t", "e")] and sleeps == [2]


async def test_a_failing_inline_job_is_logged_not_raised(serverless):
    await jobs.enqueue("bad_job", "x")  # must not raise
