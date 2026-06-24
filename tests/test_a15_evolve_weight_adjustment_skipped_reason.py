"""A15 — weight-adjustment silent-noop observability.

Gap measured: ``memclaw_evolve`` returns 200 OK with ``weight_adjustments=[]``
when scope-filter drops every related_id or the bulk UPDATE updates zero rows,
making the caller's "RL feedback was applied" assumption silently wrong.

A15 mirrors A10 (rule_skipped_reason): every silent-exit path on the
weight-adjustment flow now sets a slug from ``WEIGHT_ADJUSTMENT_SKIP_REASONS``
on a new ``weight_adjustment_skipped_reason`` field in the
``report_outcome`` response, plus emits a structured log line in the
``evolve_weight_adjustment_skipped reason=<slug> ...`` shape so a single
``grep`` finds every such event regardless of structlog ``extra={}``
rendering.

The actual weight-adjustment logic is unchanged — this is pure
observability.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_weight_adjustment_skip_reason_constants_defined():
    """Central enum so callers can pattern-match on stable slugs."""
    from core_api.services.evolve_service import WEIGHT_ADJUSTMENT_SKIP_REASONS

    for slug in (
        "no_related_ids",
        "agent_id_mismatch",
        "fleet_id_mismatch",
        "all_out_of_scope",
        "no_rows_updated",
    ):
        assert slug in WEIGHT_ADJUSTMENT_SKIP_REASONS


# ---------------------------------------------------------------------------
# Response shape — field is present on every call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_includes_weight_adjustment_skipped_reason_field():
    from core_api.services.evolve_service import report_outcome

    db = AsyncMock()
    with (
        patch(
            "core_api.services.evolve_service._adjust_weights",
            new=AsyncMock(return_value=(None, [], [])),
        ),
        patch(
            "core_api.services.evolve_service._persist_outcome",
            new=AsyncMock(return_value="00000000-0000-0000-0000-000000000001"),
        ),
    ):
        result = await report_outcome(tenant_id="t1",
            outcome="passing test",
            outcome_type="success",
            related_ids=None,
            agent_id="a1",
        )

    assert "weight_adjustment_skipped_reason" in result


# ---------------------------------------------------------------------------
# Per-path reason coverage.
# ---------------------------------------------------------------------------


async def _run(
    outcome_type,
    related_ids=None,
    scope="agent",
    fleet_id=None,
    filter_return=None,
    adjust_return=None,
    **patches,
):
    """Invoke ``report_outcome`` with sensible-default mocks; allow per-test
    override of the scope-filter and weight-adjust outputs."""
    from core_api.services.evolve_service import report_outcome

    db = AsyncMock()
    base_patches = {
        "_filter_by_scope": AsyncMock(
            return_value=filter_return
            if filter_return is not None
            else (related_ids or [], 0)
        ),
        "_adjust_weights": AsyncMock(
            return_value=adjust_return if adjust_return is not None else (None, [], [])
        ),
        "_persist_outcome": AsyncMock(
            return_value="00000000-0000-0000-0000-000000000001"
        ),
    }
    base_patches.update(patches)
    with patch.multiple(
        "core_api.services.evolve_service",
        **{k: v for k, v in base_patches.items()},
    ):
        return await report_outcome(tenant_id="t1",
            outcome="report",
            outcome_type=outcome_type,
            related_ids=related_ids,
            scope=scope,
            agent_id="a1",
            fleet_id=fleet_id,
        )


@pytest.mark.asyncio
async def test_no_related_ids_slug():
    """Caller passed no IDs → slug = no_related_ids."""
    result = await _run("failure", related_ids=None)
    assert result["weight_adjustment_skipped_reason"] == "no_related_ids"
    assert result["weight_adjustments"] == []


@pytest.mark.asyncio
async def test_no_related_ids_empty_list_slug():
    """Caller passed an empty list → same slug."""
    result = await _run("failure", related_ids=[])
    assert result["weight_adjustment_skipped_reason"] == "no_related_ids"


@pytest.mark.asyncio
async def test_agent_id_mismatch_slug():
    """scope=agent, filter drops every supplied id → agent_id_mismatch."""
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000001"],
        scope="agent",
        # filter returns empty in_scope + the full count as out_of_scope
        filter_return=([], 1),
    )
    assert result["weight_adjustment_skipped_reason"] == "agent_id_mismatch"
    assert result["weight_adjustments"] == []
    assert result["out_of_scope_count"] == 1


@pytest.mark.asyncio
async def test_fleet_id_mismatch_slug():
    """scope=fleet, filter drops every supplied id → fleet_id_mismatch."""
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000002"],
        scope="fleet",
        fleet_id="f1",
        filter_return=([], 1),
    )
    assert result["weight_adjustment_skipped_reason"] == "fleet_id_mismatch"


@pytest.mark.asyncio
async def test_all_out_of_scope_slug():
    """scope=all, filter drops every supplied id (invalid UUIDs / missing rows)
    → all_out_of_scope."""
    result = await _run(
        "failure",
        related_ids=["not-a-uuid", "also-bad"],
        scope="all",
        filter_return=([], 2),
    )
    assert result["weight_adjustment_skipped_reason"] == "all_out_of_scope"


@pytest.mark.asyncio
async def test_no_rows_updated_slug():
    """Filter passed some IDs, but ``_adjust_weights`` returned empty —
    e.g. row deleted between filter and UPDATE. Deeper-stage slug wins."""
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000003"],
        scope="agent",
        # filter passed the id through
        filter_return=(["00000000-0000-0000-0000-000000000003"], 0),
        # _adjust_weights's race path
        adjust_return=("no_rows_updated", [], []),
    )
    assert result["weight_adjustment_skipped_reason"] == "no_rows_updated"


@pytest.mark.asyncio
async def test_skip_reason_none_on_success():
    """At least one weight moved → field is None."""
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000004"],
        scope="agent",
        filter_return=(["00000000-0000-0000-0000-000000000004"], 0),
        adjust_return=(
            None,
            ["00000000-0000-0000-0000-000000000004"],
            [
                {
                    "memory_id": "00000000-0000-0000-0000-000000000004",
                    "old_weight": 0.5,
                    "new_weight": 0.55,
                    "delta": 0.05,
                }
            ],
        ),
    )
    assert result["weight_adjustment_skipped_reason"] is None
    assert len(result["weight_adjustments"]) == 1


# ---------------------------------------------------------------------------
# Structured log emission.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_emitted_with_reason_in_message_on_scope_mismatch(caplog):
    """Always-fire grep target: ``evolve_weight_adjustment_skipped reason=<slug>``."""
    import logging

    caplog.set_level(logging.INFO, logger="core_api.services.evolve_service")
    await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000001"],
        scope="agent",
        filter_return=([], 1),
    )
    assert any(
        "evolve_weight_adjustment_skipped" in rec.getMessage()
        and "reason=agent_id_mismatch" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_log_emitted_with_reason_on_no_rows_updated(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="core_api.services.evolve_service")
    await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000003"],
        scope="agent",
        filter_return=(["00000000-0000-0000-0000-000000000003"], 0),
        adjust_return=("no_rows_updated", [], []),
    )
    assert any(
        "evolve_weight_adjustment_skipped" in rec.getMessage()
        and "reason=no_rows_updated" in rec.getMessage()
        for rec in caplog.records
    )
