"""Repository for audit_log table queries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.audit import AuditLog


class AuditRepository:
    """Single point of DB access for AuditLog rows."""

    async def add(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        action: str,
        resource_type: str,
        resource_id: UUID | None = None,
        detail: dict | None = None,
    ) -> None:
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
            )
        )
        # Flushed with the caller's commit -- no separate commit here.

    async def list_by_tenant(
        self,
        db: AsyncSession,
        tenant_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[AuditLog]:
        q = (
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        if since:
            q = q.where(AuditLog.created_at > since)
        result = await db.execute(q)
        return list(result.scalars().all())
