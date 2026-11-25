import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Route = Literal["exact_lookup", "general", "action", "out_of_scope", "restricted"]
LookupKind = Literal["session", "exhibitor", "speaker", "room", "none"]
ActionName = Literal["bookmark_session", "list_bookmarks", "filter_exhibitors", "none"]


class RouteDecision(BaseModel):
    """Router output (Flash-Lite, structured)."""

    route: Route
    lookup_kind: LookupKind = "none"
    entity_query: str | None = Field(
        default=None, description="Name/title/booth fragment to look up, e.g. 'FHIR workshop'"
    )
    action: ActionName = "none"
    action_arg: str | None = Field(
        default=None, description="Session title for bookmarks, or exhibitor category/topic"
    )
    reason: str = Field(default="", max_length=300)


class GroundedAnswer(BaseModel):
    """Answer generator output (Flash, structured)."""

    answer: str = Field(max_length=2000)
    cited: list[int] = Field(default_factory=list, description="Source numbers used, e.g. [1, 3]")
    insufficient: bool = Field(description="True if the sources do not contain the answer")


class Citation(BaseModel):
    ref: int
    entity_type: str
    entity_id: uuid.UUID
    title: str
    snippet: str


class AssistantQuery(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class AssistantAnswer(BaseModel):
    answer: str
    route: Route
    citations: list[Citation] = Field(default_factory=list)
    action: ActionName | None = None
    data: Any | None = None
    cached: bool = False
    latency_ms: int = 0
