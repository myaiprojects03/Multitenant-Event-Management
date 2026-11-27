import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.analytics import router as analytics_router
from app.api.assistant import router as assistant_router
from app.api.auth import me_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.matchmaking import router as matchmaking_router
from app.api.program import router as program_router
from app.api.telemetry import router as telemetry_router
from app.api.webhooks import router as webhooks_router
from app.api.workflows import router as workflows_router
from app.core.config import get_settings
from app.core.db import assert_rls_applies, dispose_engine
from app.core.jobs import close_job_pool
from app.core.logging import configure_logging
from app.core.redis import close_redis
from app.core.telemetry import telemetry_middleware


def _start_worker():
    """The arq worker inside this process (RUN_WORKER_IN_PROCESS): for hosts without a
    separate worker service. Same jobs and cron as `arq app.workers.main.WorkerSettings`."""
    from arq.worker import Worker

    from app.workers.main import WorkerSettings as W

    worker = Worker(
        functions=W.functions,
        cron_jobs=W.cron_jobs,
        redis_settings=W.redis_settings,
        max_tries=W.max_tries,
        handle_signals=False,  # uvicorn owns signals; we stop it from the lifespan
    )
    return worker, asyncio.create_task(worker.async_run())


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.app_env == "production":
        await assert_rls_applies()  # fail the deploy rather than serve every tenant's data
    background = []
    worker = None
    if settings.run_worker_in_process:
        worker, task = _start_worker()
        background.append(task)
    if settings.demo_auto_seed:
        from app import demo

        background.append(asyncio.create_task(demo.refresh()))  # serve while it runs
    yield
    if worker is not None:
        await worker.close()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await dispose_engine()
    await close_redis()
    await close_job_pool()


app = FastAPI(
    title="Multi-Tenant Event AI Platform",
    version="0.1.0",
    lifespan=lifespan,
)
app.middleware("http")(telemetry_middleware)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(assistant_router)
app.include_router(analytics_router)
app.include_router(matchmaking_router)
app.include_router(webhooks_router)
app.include_router(workflows_router)
app.include_router(program_router)
app.include_router(telemetry_router)
