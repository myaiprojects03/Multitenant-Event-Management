from typing import Any, Literal

from pydantic import BaseModel, Field

ChartType = Literal["bar", "line", "table", "kpi"]


class SqlPlan(BaseModel):
    """Model output for one analytics question."""

    answerable: bool = Field(
        description="False if the question cannot be answered from the views or asks to change data"
    )
    sql: str = Field(default="", description="One PostgreSQL SELECT over the approved views")
    explanation: str = Field(default="", max_length=600, description="How the query answers it")
    chart_type: ChartType = "table"
    x: str | None = Field(default=None, description="Result column for the x axis / categories")
    y: list[str] = Field(default_factory=list, description="Numeric result column(s) to plot")


class Summary(BaseModel):
    summary: str = Field(max_length=800, description="2-3 sentence executive summary")
    key_points: list[str] = Field(default_factory=list, max_length=4)


class Chart(BaseModel):
    type: ChartType
    x: str | None = None
    y: list[str] = Field(default_factory=list)


class NlSqlRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


Status = Literal["ok", "unanswerable", "blocked", "failed"]


class NlSqlResponse(BaseModel):
    question: str
    status: Status
    sql: str | None = None
    explanation: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: Chart | None = None
    repaired: bool = False
    error: str | None = None
    cached: bool = False
    latency_ms: int = 0


class AnomalyFlag(BaseModel):
    kind: Literal["capacity_risk", "dropoff", "lead_velocity"]
    severity: Literal["medium", "high"]
    title: str
    metric: str
    value: float
    baseline: float | None = None
    detail: str
    entity_id: str | None = None


class AnomalyReport(BaseModel):
    flags: list[AnomalyFlag]
    explanation: str = ""
    cached: bool = False
