"""Evolve cannot use a peer's private memory in a shared rule or weight change."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from common.models import Memory
from core_api.services import evolve_service
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed(tenant_id, agent_id, visibility, *, fleet_id="f1", deleted=False):
    memory_id = uuid4()
    async with get_session() as session:
        session.add(
            Memory(
                id=memory_id,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id=agent_id,
                content=f"evolve visibility fixture {memory_id}",
                memory_type="fact",
                status="active",
                visibility=visibility,
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
    return str(memory_id)


@pytest.mark.parametrize("scope", ["agent", "fleet", "all"])
async def test_scope_filter_preserves_visibility_and_existing_boundaries(scope):
    tenant = f"test-tenant-evolve-visibility-{uuid4().hex[:8]}"
    own = await _seed(tenant, "caller", "scope_agent")
    private_peer = await _seed(tenant, "peer", "scope_agent")
    team = await _seed(tenant, "peer", "scope_team")
    org = await _seed(tenant, "peer", "scope_org")
    other_fleet = await _seed(tenant, "peer", "scope_team", fleet_id="f2")
    foreign = await _seed(f"{tenant}-other", "caller", "scope_agent")
    deleted = await _seed(tenant, "caller", "scope_team", deleted=True)
    unknown_visibility = await _seed(tenant, "caller", "unknown")
    ids = [
        own,
        private_peer,
        team,
        org,
        other_fleet,
        foreign,
        deleted,
        unknown_visibility,
    ]

    allowed, dropped = await evolve_service._filter_by_scope(
        tenant_id=tenant,
        caller_agent_id="caller",
        fleet_id="f1" if scope == "fleet" else None,
        scope=scope,
        related_ids=ids,
    )

    expected = {own}
    if scope in {"fleet", "all"}:
        expected.update({team, org})
    if scope == "all":
        expected.add(other_fleet)
    assert set(allowed) == expected
    assert dropped == len(ids) - len(expected)


@pytest.mark.parametrize("scope", ["fleet", "all"])
async def test_peer_private_ids_never_reach_rule_generation_or_persistence(
    monkeypatch, scope
):
    tenant = f"test-tenant-evolve-prompt-{uuid4().hex[:8]}"
    private_peer = await _seed(tenant, "peer", "scope_agent")
    shared = await _seed(tenant, "peer", "scope_team")
    generate = AsyncMock(return_value=(None, "llm_failed"))
    persist = AsyncMock(return_value={})
    monkeypatch.setattr(evolve_service, "_maybe_generate_rule", generate)
    monkeypatch.setattr(evolve_service, "_apply_outcome_to_db", persist)

    await evolve_service.report_outcome(
        tenant_id=tenant,
        outcome="The shared procedure failed.",
        outcome_type="failure",
        related_ids=[private_peer, shared],
        scope=scope,
        agent_id="caller",
        fleet_id="f1" if scope == "fleet" else None,
    )

    generate.assert_awaited_once()
    assert generate.await_args.args[3] == [shared]
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["related_ids"] == [shared]
    assert persist.await_args.kwargs["out_of_scope_count"] == 1
