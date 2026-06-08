"""Repository for analysis_reports table queries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.analysis_report import CrystallizationReport


class ReportRepository:
    """Single point of DB access for CrystallizationReport rows."""

    async def get_by_id(self, db: AsyncSession, report_id: UUID) -> CrystallizationReport | None:
        return await db.get(CrystallizationReport, report_id)

    async def find_running(self, db: AsyncSession, tenant_id: str, fleet_id: str | None) -> UUID | None:
        result = await db.execute(
            select(CrystallizationReport.id).where(
                CrystallizationReport.tenant_id == tenant_id,
                CrystallizationReport.fleet_id == fleet_id
                if fleet_id
                else CrystallizationReport.fleet_id.is_(None),
                CrystallizationReport.status == "running",
            )
        )
        return result.scalar_one_or_none()

    async def add(self, db: AsyncSession, report: CrystallizationReport) -> None:
        db.add(report)
        await db.flush()

    async def update_completed(
        self,
        db: AsyncSession,
        report_id: UUID,
        *,
        status: str,
        completed_at: datetime,
        duration_ms: int,
        summary: dict,
        hygiene: dict,
        health: dict,
        usage_data: dict,
        issues: list,
        crystallization: dict,
    ) -> None:
        await db.execute(
            update(CrystallizationReport)
            .where(CrystallizationReport.id == report_id)
            .values(
                status=status,
                completed_at=completed_at,
                duration_ms=duration_ms,
                summary=summary,
                hygiene=hygiene,
                health=health,
                usage_data=usage_data,
                issues=issues,
                crystallization=crystallization,
            )
        )

    async def list_by_tenant(
        self,
        db: AsyncSession,
        tenant_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[CrystallizationReport]:
        result = await db.execute(
            select(CrystallizationReport)
            .where(CrystallizationReport.tenant_id == tenant_id)
            .order_by(CrystallizationReport.started_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_latest_completed(self, db: AsyncSession, tenant_id: str) -> CrystallizationReport | None:
        result = await db.execute(
            select(CrystallizationReport)
            .where(
                CrystallizationReport.tenant_id == tenant_id,
                CrystallizationReport.status == "completed",
            )
            .order_by(CrystallizationReport.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
