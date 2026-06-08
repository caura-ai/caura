"""Repository for fleet_nodes and fleet_commands table queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.fleet import FleetCommand, FleetNode


class FleetRepository:
    """Single point of DB access for FleetNode and FleetCommand rows."""

    # ── Fleet CRUD ──

    async def fleet_exists(self, db: AsyncSession, *, tenant_id: str, fleet_id: str) -> bool:
        result = await db.execute(
            select(FleetNode.id)
            .where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.fleet_id == fleet_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_fleets(self, db: AsyncSession, *, tenant_id: str) -> Sequence[Any]:
        """Return rows of (fleet_id, node_count, last_heartbeat)."""
        result = await db.execute(
            select(
                FleetNode.fleet_id,
                func.sum(
                    case(
                        (~FleetNode.node_name.startswith("_fleet_"), 1),
                        else_=0,
                    )
                ).label("node_count"),
                func.max(FleetNode.last_heartbeat).label("last_heartbeat"),
            )
            .where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.fleet_id.isnot(None),
            )
            .group_by(FleetNode.fleet_id)
            .order_by(FleetNode.fleet_id)
        )
        return result.all()

    async def delete_fleet(self, db: AsyncSession, *, tenant_id: str, fleet_id: str) -> None:
        """Delete all nodes (and their commands) for a fleet."""
        node_ids = await self.get_node_ids_for_fleet(db, tenant_id=tenant_id, fleet_id=fleet_id)
        if node_ids:
            await self.delete_commands_for_nodes(db, node_ids=node_ids)

        await db.execute(
            FleetNode.__table__.delete().where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.fleet_id == fleet_id,
            )
        )

    # ── Nodes ──

    async def upsert_node(self, db: AsyncSession, *, values: dict[str, Any]) -> UUID:
        stmt = pg_insert(FleetNode.__table__).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fleet_nodes_tenant_node",
            set_={k: v for k, v in values.items() if k not in ("tenant_id", "node_name")},
        ).returning(FleetNode.__table__.c.id)
        result = await db.execute(stmt)
        await db.flush()
        return result.scalar_one()

    async def add_node(self, db: AsyncSession, *, node: FleetNode) -> None:
        db.add(node)

    async def get_node_id(self, db: AsyncSession, *, tenant_id: str, node_name: str) -> UUID:
        result = await db.execute(
            select(FleetNode.id).where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.node_name == node_name,
            )
        )
        return result.scalar_one()

    async def get_node_by_id(self, db: AsyncSession, *, node_id: UUID) -> FleetNode | None:
        return await db.get(FleetNode, node_id)

    async def list_nodes(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> Sequence[FleetNode]:
        query = select(FleetNode).where(FleetNode.tenant_id == tenant_id)
        if fleet_id:
            query = query.where(FleetNode.fleet_id == fleet_id)
        result = await db.execute(query.order_by(FleetNode.last_heartbeat.desc()))
        return result.scalars().all()

    async def count_nodes(self, db: AsyncSession, *, tenant_id: str, fleet_id: str) -> int:
        result = await db.execute(
            select(func.count(FleetNode.id)).where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.fleet_id == fleet_id,
            )
        )
        return result.scalar() or 0

    async def get_node_ids_for_fleet(self, db: AsyncSession, *, tenant_id: str, fleet_id: str) -> list[UUID]:
        result = await db.execute(
            select(FleetNode.id).where(
                FleetNode.tenant_id == tenant_id,
                FleetNode.fleet_id == fleet_id,
            )
        )
        return list(result.scalars().all())

    # ── Commands ──

    async def get_command_by_id(self, db: AsyncSession, *, command_id: UUID) -> FleetCommand | None:
        return await db.get(FleetCommand, command_id)

    async def get_pending_commands(self, db: AsyncSession, *, node_id: UUID) -> Sequence[FleetCommand]:
        result = await db.execute(
            select(FleetCommand)
            .where(
                FleetCommand.node_id == node_id,
                FleetCommand.status == "pending",
            )
            .order_by(FleetCommand.created_at)
        )
        return result.scalars().all()

    async def has_recent_in_flight_deploy(
        self,
        db: AsyncSession,
        *,
        node_id: UUID,
        since: datetime,
    ) -> bool:
        """True if a ``deploy`` command for this node is still in flight.

        "In flight" means status in (``pending``, ``acked``) and
        ``created_at >= since``. Used by the auto-upgrade gate to suppress
        queueing duplicate deploys when a previous one has been sent to
        the plugin but never reported back as completed/failed.

        Why this matters (CAURA-000): the existing pending-only gate
        (``_has_recent_deploy_command_from_list``) is blind to ``acked``
        commands. After the heartbeat handler transitions a command
        ``pending`` → ``acked`` and ships it to the plugin, the gate
        sees an empty pending list on the next heartbeat and happily
        queues another deploy — even if the plugin's last deploy is
        still in flight or got killed mid-result-POST by its own
        ``systemctl restart``. Customer prod-data observation
        (2026-06-08): 1,381 deploy commands stuck at ``acked``, one new
        per heartbeat, on a single node — exactly the customer-reported
        "SIGTERM every 60s" cycle.

        The ``since`` window bounds the gate: an acked command older
        than ~10 min is assumed abandoned, and a retry is allowed. This
        recovers nodes whose plugin crashed mid-deploy (lost result-POST)
        without needing operator intervention to clear stale rows.
        """
        result = await db.execute(
            select(FleetCommand.id)
            .where(
                FleetCommand.node_id == node_id,
                FleetCommand.command == "deploy",
                FleetCommand.status.in_(("pending", "acked")),
                FleetCommand.created_at >= since,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def ack_commands(self, db: AsyncSession, *, command_ids: list[UUID], now: datetime) -> None:
        if not command_ids:
            return
        await db.execute(
            update(FleetCommand).where(FleetCommand.id.in_(command_ids)).values(status="acked", acked_at=now)
        )

    async def add_command(self, db: AsyncSession, *, command: FleetCommand) -> None:
        db.add(command)

    async def list_commands(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        node_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[FleetCommand]:
        stmt = (
            select(FleetCommand)
            .where(FleetCommand.tenant_id == tenant_id)
            .order_by(FleetCommand.created_at.desc())
            .limit(limit)
        )
        if node_id:
            stmt = stmt.where(FleetCommand.node_id == node_id)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def delete_commands_for_nodes(self, db: AsyncSession, *, node_ids: list[UUID]) -> None:
        if not node_ids:
            return
        await db.execute(FleetCommand.__table__.delete().where(FleetCommand.node_id.in_(node_ids)))
