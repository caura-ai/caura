"""Step protocol and result types for pipeline execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class StepOutcome(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class StepResult:
    outcome: StepOutcome
    error: Exception | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Step(Protocol):
    """A single pipeline step.

    Implementations must define ``name`` and ``execute()``.
    Steps that raise HTTPException propagate it directly (preserving
    existing 409/422/504 behavior). Other exceptions are caught by the
    runner and wrapped in a StepResult(FAILED).
    """

    @property
    def name(self) -> str: ...

    async def execute(self, ctx: Any) -> StepResult | None:
        """Run the step.

        Returns None for implicit success or a StepResult for
        explicit outcome reporting.
        """
        ...
