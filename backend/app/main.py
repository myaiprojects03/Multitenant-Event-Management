from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import me_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging
from app.core.redis import close_redis


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(get_settings().log_level)
    yield
    await dispose_engine()
    await close_redis()


app = FastAPI(
    title="Multi-Tenant Event AI Platform",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(me_router)
