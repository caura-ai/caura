"""Usage tracking stubs for OSS standalone mode.

In enterprise mode, usage enforcement is handled by the enterprise platform-admin-api.
In OSS standalone mode, there are no usage limits — all operations are allowed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

OperationType = Literal["write", "search", "recall", "insights", "evolve"]


@dataclass
class UsageCheckResult:
    allowed: bool
    operation: str
    current: int
    limit: int | None
    remaining: int | None
    resets_at: datetime | None
    plan: str = "free"

    def get(self, key: str, default=None):
        """Dict-style access for backward compatibility with route code."""
        return getattr(self, key, default)


def _allowed(op: str) -> UsageCheckResult:
    return UsageCheckResult(
        allowed=True,
        operation=op,
        current=0,
        limit=None,
        remaining=None,
        resets_at=None,
    )


async def check_and_increment(
    db: AsyncSession,
    org_id,
    operation: OperationType,
    count: int = 1,
) -> UsageCheckResult:
    return _allowed(operation)


async def check_and_increment_by_tenant(
    db: AsyncSession,
    tenant_id: str,
    operation: OperationType,
    count: int = 1,
) -> UsageCheckResult:
    return _allowed(operation)


async def bulk_check_and_increment(
    db: AsyncSession,
    tenant_id: str,
    count: int,
) -> UsageCheckResult:
    return _allowed("write")
