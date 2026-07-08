"""Regression tests for _CoreApiLifecycleAdapter.crystallize (CAURA-657).

The lifecycle crystallize adapter previously called run_crystallization as
``run_crystallization(None, org_id, fleet_id, trigger="lifecycle")`` — a stray
leading positional that shifted every arg and passed ``trigger`` both
positionally and by keyword, so every lifecycle-triggered crystallization raised
``TypeError: got multiple values for argument 'trigger'`` and the pubsub handler
nacked + redelivered forever in prod (observed ~20x/day). The existing
lifecycle-handler tests use a fully-faked adapter, so the real call site was
never exercised. These tests hit the real adapter method with an ``autospec``'d
run_crystallization, so the enforced signature makes a regression fail here
rather than in a live pubsub handler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services.lifecycle_audit import (
    _CRYSTALLIZE_MIN_ACTIVE_MEMORIES,
    _CoreApiLifecycleAdapter,
)


class _FakeStorage:
    def __init__(self, active: int) -> None:
        self._active = active

    async def count_active(self, org_id: str, fleet_id: str | None) -> int:
        return self._active


class _Cfg:
    auto_crystallize_enabled = True


@pytest.mark.asyncio
async def test_crystallize_calls_run_crystallization_with_correct_args() -> None:
    adapter = _CoreApiLifecycleAdapter(
        _FakeStorage(active=_CRYSTALLIZE_MIN_ACTIVE_MEMORIES + 1)
    )
    report_id = uuid4()
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_Cfg()),
        ),
        patch(
            "core_api.services.crystallizer_service.run_crystallization",
            autospec=True,
        ) as mock_run,
    ):
        mock_run.return_value = report_id
        result = await adapter.crystallize(org_id="t1", fleet_id="f1")

    assert result == 1
    # org_id maps to run_crystallization's tenant_id (translated at the boundary);
    # trigger is passed exactly once. Under autospec the old buggy positional call
    # (None, org_id, fleet_id, trigger=...) would raise TypeError here.
    mock_run.assert_awaited_once_with(
        tenant_id="t1", fleet_id="f1", trigger="lifecycle"
    )


@pytest.mark.asyncio
async def test_crystallize_skips_when_auto_disabled() -> None:
    class _Off:
        auto_crystallize_enabled = False

    adapter = _CoreApiLifecycleAdapter(_FakeStorage(active=10_000))
    with patch(
        "core_api.services.lifecycle_audit.resolve_config",
        new=AsyncMock(return_value=_Off()),
    ):
        assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0


@pytest.mark.asyncio
async def test_crystallize_skips_below_active_threshold() -> None:
    adapter = _CoreApiLifecycleAdapter(
        _FakeStorage(active=_CRYSTALLIZE_MIN_ACTIVE_MEMORIES)
    )
    with patch(
        "core_api.services.lifecycle_audit.resolve_config",
        new=AsyncMock(return_value=_Cfg()),
    ):
        assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0


@pytest.mark.asyncio
async def test_insights_attributes_and_registers_dedicated_agent() -> None:
    """The automated insights run writes under the dedicated ``memclaw-insighter``
    identity — never the anonymous ``mcp-agent`` fallback — and self-registers it
    as a service agent (trust 3) for the org before persisting."""
    registered: list[dict] = []

    class _InsightsStorage:
        async def insights_activity_gate(self, *, tenant_id: str, fleet_id):
            # Corpus has grown since the last insight → run proceeds.
            return {"latest_non_insight": "2026-07-08T00:00:00+00:00", "latest_insight": None}

        async def get_agent(self, agent_id: str, tenant_id: str) -> dict | None:
            return None  # not yet registered → should create

        async def create_or_update_agent(self, payload: dict) -> dict:
            registered.append(payload)
            return {"id": str(uuid4())}

    class _On:
        auto_insights_enabled = True

    adapter = _CoreApiLifecycleAdapter(_InsightsStorage())
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_On()),
        ),
        patch(
            "core_api.services.insights_service.generate_insights",
            new=AsyncMock(return_value={"insight_memory_ids": ["a", "b"]}),
        ) as mock_gen,
    ):
        produced = await adapter.insights(org_id="t1", fleet_id=None)

    assert produced == 2
    # Attributed to the dedicated identity, tenant-wide (no fleet → scope='all').
    mock_gen.assert_awaited_once()
    assert mock_gen.await_args.kwargs["agent_id"] == "memclaw-insighter"
    assert mock_gen.await_args.kwargs["scope"] == "all"
    # Self-registered exactly once as a service agent with cross-fleet trust.
    assert len(registered) == 1
    reg = registered[0]
    assert reg["tenant_id"] == "t1"
    assert reg["agent_id"] == "memclaw-insighter"
    assert reg["belonging_type"] == "service"
    assert reg["trust_level"] == 3


@pytest.mark.asyncio
async def test_insights_does_not_reregister_existing_agent() -> None:
    """Registration is one-time setup: when the insighter already exists, the run
    must NOT re-upsert it — create_or_update_agent's conflict path updates
    trust_level/display_name and would clobber operator customisation nightly."""

    class _InsightsStorage:
        async def insights_activity_gate(self, *, tenant_id: str, fleet_id):
            return {"latest_non_insight": "2026-07-08T00:00:00+00:00", "latest_insight": None}

        async def get_agent(self, agent_id: str, tenant_id: str) -> dict | None:
            # Already registered (operator may have since customised it).
            return {"agent_id": agent_id, "tenant_id": tenant_id, "trust_level": 2}

        async def create_or_update_agent(self, payload: dict) -> dict:  # pragma: no cover
            raise AssertionError("must not re-register an existing insighter agent")

    class _On:
        auto_insights_enabled = True

    adapter = _CoreApiLifecycleAdapter(_InsightsStorage())
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_On()),
        ),
        patch(
            "core_api.services.insights_service.generate_insights",
            new=AsyncMock(return_value={"insight_memory_ids": ["a"]}),
        ) as mock_gen,
    ):
        produced = await adapter.insights(org_id="t1", fleet_id=None)

    assert produced == 1
    # Still attributed to the dedicated identity even though it was pre-existing.
    assert mock_gen.await_args.kwargs["agent_id"] == "memclaw-insighter"


@pytest.mark.asyncio
async def test_insights_registration_failure_does_not_abort_run() -> None:
    """A transient storage error while registering the insighter must NOT abort
    the run: the insight write works without the agent row, so the run completes
    and the next nightly pass retries registration."""

    class _InsightsStorage:
        async def insights_activity_gate(self, *, tenant_id: str, fleet_id):
            return {"latest_non_insight": "2026-07-08T00:00:00+00:00", "latest_insight": None}

        async def get_agent(self, agent_id: str, tenant_id: str) -> dict | None:
            raise RuntimeError("storage down")

        async def create_or_update_agent(self, payload: dict) -> dict:  # pragma: no cover
            raise AssertionError("unreachable — get_agent raised first")

    class _On:
        auto_insights_enabled = True

    adapter = _CoreApiLifecycleAdapter(_InsightsStorage())
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_On()),
        ),
        patch(
            "core_api.services.insights_service.generate_insights",
            new=AsyncMock(return_value={"insight_memory_ids": ["a", "b", "c"]}),
        ) as mock_gen,
    ):
        produced = await adapter.insights(org_id="t1", fleet_id=None)

    # Run completed despite the registration failure, still under the dedicated id.
    assert produced == 3
    assert mock_gen.await_args.kwargs["agent_id"] == "memclaw-insighter"


@pytest.mark.asyncio
async def test_insights_skips_registration_when_auto_disabled() -> None:
    """Insights opt-out short-circuits before any registration or generation —
    a tenant that never runs insights gets no phantom agent row."""

    class _Off:
        auto_insights_enabled = False

    class _Storage:
        async def create_or_update_agent(self, payload: dict) -> dict:  # pragma: no cover
            raise AssertionError("must not register when auto_insights disabled")

    adapter = _CoreApiLifecycleAdapter(_Storage())
    with patch(
        "core_api.services.lifecycle_audit.resolve_config",
        new=AsyncMock(return_value=_Off()),
    ):
        assert await adapter.insights(org_id="t1", fleet_id=None) == 0
