"""Repository for agents table queries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.agent import Agent


class AgentRepository:
    """Single point of DB access for Agent rows."""

    async def get_by_id(
        self,
        db: AsyncSession,
        agent_id: str,
        tenant_id: str,
    ) -> Agent | None:
        result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_id == agent_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        db: AsyncSession,
        tenant_id: str,
    ) -> list[Agent]:
        result = await db.execute(
            select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at.desc())
        )
        return list(result.scalars().all())

    async def add(self, db: AsyncSession, agent: Agent) -> None:
        db.add(agent)
        await db.flush()

    async def delete(self, db: AsyncSession, agent: Agent) -> None:
        await db.delete(agent)

    async def update_trust_level(
        self,
        db: AsyncSession,
        agent: Agent,
        trust_level: int,
        fleet_id: str | None = None,
    ) -> None:
        agent.trust_level = trust_level
        if fleet_id is not None:
            agent.fleet_id = fleet_id
        agent.updated_at = datetime.now(UTC)
        await db.flush()

    async def update_fleet(
        self,
        db: AsyncSession,
        agent: Agent,
        fleet_id: str,
    ) -> None:
        agent.fleet_id = fleet_id

    async def update_search_profile(
        self,
        db: AsyncSession,
        agent_id_pk: object,
        search_profile: dict,
    ) -> None:
        """Update an agent's search_profile by primary key (Agent.id)."""
        await db.execute(
            sql_update(Agent).where(Agent.id == agent_id_pk).values(search_profile=search_profile)
        )

    async def reset_search_profile(
        self,
        db: AsyncSession,
        agent_id_pk: object,
    ) -> None:
        """Clear an agent's search_profile by primary key (Agent.id)."""
        await db.execute(sql_update(Agent).where(Agent.id == agent_id_pk).values(search_profile=None))

    async def backfill_from_memories(self, db: AsyncSession) -> int:
        """Create agent rows for (tenant_id, agent_id) pairs in memories
        that don't have an agent row yet."""
        result = await db.execute(
            sql_text("""
            INSERT INTO agents (tenant_id, agent_id, fleet_id, trust_level)
            SELECT DISTINCT ON (m.tenant_id, m.agent_id)
                   m.tenant_id, m.agent_id,
                   m.fleet_id,
                   1
            FROM memories m
            WHERE m.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM agents a
                  WHERE a.tenant_id = m.tenant_id AND a.agent_id = m.agent_id
              )
            ORDER BY m.tenant_id, m.agent_id, m.created_at ASC
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
        """)
        )
        await db.flush()
        return result.rowcount
