from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseResult:
    id: str
    passed: bool
    detail: str = ""
    leak: bool = False  # data the persona must not see reached them
    error: bool = False  # infrastructure failure (LLM unavailable, HTTP 5xx), not a wrong answer
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    name: str
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def leaks(self) -> int:
        return sum(c.leak for c in self.cases)

    @property
    def errors(self) -> int:
        return sum(c.error for c in self.cases)


ONLY: set[str] | None = None  # --rerun-errors: run just these case ids


def sample(cases: list, n: int | None) -> list:
    """Every k-th case, so a sample still covers each section of a golden file."""
    if ONLY is not None:
        return [c for c in cases if c.get("id") in ONLY]
    if not n or n >= len(cases):
        return cases
    step = len(cases) / n
    return [cases[int(i * step)] for i in range(n)]
