from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.analytics import router as analytics_router
from app.api.assistant import router as assistant_router
from app.api.auth import me_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.telemetry import router as telemetry_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.jobs import close_job_pool
from app.core.logging import configure_logging
from app.core.redis import close_redis
from app.core.telemetry import telemetry_middleware


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(get_settings().log_level)
    yield
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
app.include_router(webhooks_router)
app.include_router(telemetry_router)
