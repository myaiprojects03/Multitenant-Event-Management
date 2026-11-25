from typing import Annotated

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.core.embeddings import get_embedder
from app.core.llm import get_llm
from app.core.ratelimit import rate_limited
from app.core.redis import get_redis
from app.core.security import CtxDep, DbDep
from app.modules.assistant.schemas import AssistantAnswer, AssistantQuery
from app.modules.assistant.service import AssistantService

router = APIRouter(prefix="/assistant", tags=["assistant"])


def get_assistant(redis: Annotated[Redis, Depends(get_redis)]) -> AssistantService:
    return AssistantService(get_llm(), get_embedder(), redis)


@router.post("/query", response_model=AssistantAnswer, dependencies=[Depends(rate_limited("llm"))])
async def query(
    body: AssistantQuery,
    ctx: CtxDep,
    db: DbDep,
    assistant: Annotated[AssistantService, Depends(get_assistant)],
) -> AssistantAnswer:
    """Ask the event assistant. Scope (tenant, event, role, user) comes from the token only."""
    return await assistant.answer(db, ctx, body.question)
