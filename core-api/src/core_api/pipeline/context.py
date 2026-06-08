"""Pipeline execution context — shared state passed through all steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class PipelineContext:
    db: AsyncSession | None = None
    data: dict[str, Any] = field(default_factory=dict)
    tenant_config: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def require_db(self) -> AsyncSession:
        """Return the DB session or raise if unavailable (STM path)."""
        if self.db is None:
            raise RuntimeError(
                "This pipeline step requires a DB session. PipelineContext was built without one (STM path)."
            )
        return self.db
