"""Repository for background_task_log table queries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from common.models.background_task import BackgroundTaskLog


class TaskRepository:
    """Single point of DB access for BackgroundTaskLog rows."""

    async def add_failure(
        self,
        db: AsyncSession,
        *,
        task_name: str,
        memory_id: UUID | None = None,
        tenant_id: str,
        error_message: str,
        error_traceback: str,
    ) -> None:
        db.add(
            BackgroundTaskLog(
                task_name=task_name,
                memory_id=memory_id,
                tenant_id=tenant_id,
                status="failed",
                error_message=error_message[:1000],
                error_traceback=error_traceback,
                completed_at=datetime.now(UTC),
            )
        )
        await db.flush()
