"""Tests for the evolve service — unit + integration.

Unit tests (no DB): validation, fake rule, delta/weight math.
Integration tests (require DB): weight adjustment, outcome persistence, rule generation.
"""

import uuid

import pytest

from tests._mcp_test_helpers import as_text
from tests.conftest import uid as _uid


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestOutcomeTypeValidation:
    """Test that invalid outcome_type is rejected before hitting the DB.

    The service raises ValueError (not HTTPException) so it stays
    decoupled from FastAPI — REST/MCP callers translate to their layer's
    error convention.
    """

    @pytest.mark.asyncio
    async def test_invalid_outcome_type_raises(self):
        from core_api.services.evolve_service import report_outcome

        with pytest.raises(ValueError) as exc_info:
            await report_outcome(None, "t1", outcome="test", outcome_type="invalid")
        assert "invalid" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_valid_outcome_types_accepted(self):
        """Valid types should not raise on validation (may fail later on DB access)."""
        from core_api.constants import EVOLVE_OUTCOME_TYPES

        assert "success" in EVOLVE_OUTCOME_TYPES
        assert "failure" in EVOLVE_OUTCOME_TYPES
        assert "partial" in EVOLVE_OUTCOME_TYPES


class TestFakeRule:
    """Test _fake_rule returns valid structure."""

    def test_structure(self):
        from core_api.services.evolve_service import _fake_rule

        result = _fake_rule()
        assert isinstance(result, dict)
        assert "condition" in result
        assert "action" in result
        assert "confidence" in result
        assert "reasoning" in result
        assert isinstance(result["confidence"], (int, float))
        assert 0.0 <= result["confidence"] <= 1.0


class TestDeltaConstants:
    """Test the asymmetric delta design."""

    def test_success_positive(self):
        from core_api.constants import EVOLVE_SUCCESS_DELTA

        assert EVOLVE_SUCCESS_DELTA > 0

    def test_failure_negative(self):
        from core_api.constants import EVOLVE_FAILURE_DELTA

        assert EVOLVE_FAILURE_DELTA < 0

    def test_partial_positive_small(self):
        from core_api.constants import EVOLVE_PARTIAL_DELTA, EVOLVE_SUCCESS_DELTA

        assert EVOLVE_PARTIAL_DELTA > 0
        assert EVOLVE_PARTIAL_DELTA < EVOLVE_SUCCESS_DELTA

    def test_failure_stronger_than_success(self):
        """Failures should propagate faster than successes."""
        from core_api.constants import EVOLVE_FAILURE_DELTA, EVOLVE_SUCCESS_DELTA

        assert abs(EVOLVE_FAILURE_DELTA) > abs(EVOLVE_SUCCESS_DELTA)


class TestWeightBounds:
    """Test that weight calculations respect floor and cap."""

    def test_floor(self):
        from core_api.constants import (
            EVOLVE_FAILURE_DELTA,
            EVOLVE_WEIGHT_FLOOR,
            EVOLVE_WEIGHT_CAP,
        )

        # Starting at 0.1, failure delta = -0.15 → should floor at 0.05
        old_weight = 0.1
        new_weight = max(
            EVOLVE_WEIGHT_FLOOR,
            min(EVOLVE_WEIGHT_CAP, old_weight + EVOLVE_FAILURE_DELTA),
        )
        assert new_weight == EVOLVE_WEIGHT_FLOOR

    def test_cap(self):
        from core_api.constants import (
            EVOLVE_SUCCESS_DELTA,
            EVOLVE_WEIGHT_CAP,
            EVOLVE_WEIGHT_FLOOR,
        )

        # Starting at 0.95, success delta = +0.1 → should cap at 1.0
        old_weight = 0.95
        new_weight = max(
            EVOLVE_WEIGHT_FLOOR,
            min(EVOLVE_WEIGHT_CAP, old_weight + EVOLVE_SUCCESS_DELTA),
        )
        assert new_weight == EVOLVE_WEIGHT_CAP

    def test_no_negative_weight(self):
        from core_api.constants import (
            EVOLVE_FAILURE_DELTA,
            EVOLVE_WEIGHT_CAP,
            EVOLVE_WEIGHT_FLOOR,
        )

        # Even at absolute minimum starting weight
        old_weight = 0.05
        new_weight = max(
            EVOLVE_WEIGHT_FLOOR,
            min(EVOLVE_WEIGHT_CAP, old_weight + EVOLVE_FAILURE_DELTA),
        )
        assert new_weight >= EVOLVE_WEIGHT_FLOOR
        assert new_weight > 0


# ---------------------------------------------------------------------------
# Integration tests — require DB
# ---------------------------------------------------------------------------


async def _create_test_memory_via_sc(
    sc, tenant_id, agent_id="evolve-test-agent", weight=0.5, fleet_id=None
):
    """Create a memory via storage client (committed, visible across sessions).

    Also ensures the agent is registered so enforce_update passes.
    """
    tag = _uid()
    fid = fleet_id or f"evolve-fleet-{tag}"

    # Ensure agent exists in DB (enforce_update checks agent registration)
    await sc.create_or_update_agent(
        {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "trust_level": 1,
            "fleet_id": fid,
        }
    )

    payload = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": fid,
        "memory_type": "fact",
        "content": f"Test memory for evolve [{tag}]",
        "weight": weight,
        "status": "active",
        "recall_count": 0,
        "visibility": "scope_team",
    }
    mem = await sc.create_memory(payload)
    return str(mem["id"]), tag


async def _create_test_memory(
    db, tenant_id, agent_id="evolve-test-agent", weight=0.5, fleet_id=None
):
    """Create a memory directly in DB session (for tests that don't need cross-session visibility)."""
    from common.models.memory import Memory

    tag = _uid()
    mem = Memory(
        tenant_id=tenant_id,
        agent_id=agent_id,
        fleet_id=fleet_id or f"evolve-fleet-{tag}",
        memory_type="fact",
        content=f"Test memory for evolve [{tag}]",
        weight=weight,
        status="active",
        recall_count=0,
        visibility="scope_team",
    )
    db.add(mem)
    await db.flush()
    return str(mem.id), tag


@pytest.mark.asyncio
async def test_evolve_success_increases_weight(db, sc):
    """Report success on a memory → weight increases by SUCCESS_DELTA."""
    from core_api.constants import EVOLVE_SUCCESS_DELTA

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [mid], "success", "evolve-test-agent"
    )

    assert len(adjustments) == 1
    assert adjustments[0]["old_weight"] == 0.5
    assert adjustments[0]["new_weight"] == pytest.approx(
        0.5 + EVOLVE_SUCCESS_DELTA, abs=0.01
    )
    assert adjustments[0]["delta"] == pytest.approx(EVOLVE_SUCCESS_DELTA, abs=0.01)


@pytest.mark.asyncio
async def test_evolve_failure_decreases_weight(db, sc):
    """Report failure on a memory → weight decreases by FAILURE_DELTA."""
    from core_api.constants import EVOLVE_FAILURE_DELTA

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [mid], "failure", "evolve-test-agent"
    )

    assert len(adjustments) == 1
    assert adjustments[0]["old_weight"] == 0.5
    assert adjustments[0]["new_weight"] == pytest.approx(
        0.5 + EVOLVE_FAILURE_DELTA, abs=0.01
    )


@pytest.mark.asyncio
async def test_evolve_partial_slight_increase(db, sc):
    """Report partial on a memory → weight increases by PARTIAL_DELTA."""
    from core_api.constants import EVOLVE_PARTIAL_DELTA

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [mid], "partial", "evolve-test-agent"
    )

    assert len(adjustments) == 1
    assert adjustments[0]["new_weight"] == pytest.approx(
        0.5 + EVOLVE_PARTIAL_DELTA, abs=0.01
    )


@pytest.mark.asyncio
async def test_evolve_weight_floor(db, sc):
    """Weight never goes below EVOLVE_WEIGHT_FLOOR."""
    from core_api.constants import EVOLVE_WEIGHT_FLOOR

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.1)

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [mid], "failure", "evolve-test-agent"
    )

    assert len(adjustments) == 1
    assert adjustments[0]["new_weight"] == EVOLVE_WEIGHT_FLOOR


@pytest.mark.asyncio
async def test_evolve_weight_cap(db, sc):
    """Weight never goes above EVOLVE_WEIGHT_CAP."""
    from core_api.constants import EVOLVE_WEIGHT_CAP

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.95)

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [mid], "success", "evolve-test-agent"
    )

    assert len(adjustments) == 1
    assert adjustments[0]["new_weight"] == EVOLVE_WEIGHT_CAP


@pytest.mark.asyncio
async def test_evolve_nonexistent_memory_skipped(db):
    """Non-existent memory UUID is skipped gracefully."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    fake_id = "00000000-0000-0000-0000-000000000000"

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, [fake_id], "success", "test-agent"
    )
    assert len(adjustments) == 0


@pytest.mark.asyncio
async def test_evolve_invalid_uuid_skipped(db):
    """Invalid UUID string is skipped gracefully."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    from core_api.services.evolve_service import _adjust_weights

    _, _, adjustments = await _adjust_weights(
        db, tenant_id, ["not-a-uuid"], "success", "test-agent"
    )
    assert len(adjustments) == 0


@pytest.mark.asyncio
async def test_evolve_truncates_related_ids_above_cap(db, caplog):
    """related_ids longer than EVOLVE_MAX_RELATED_IDS are truncated with a warning."""
    import logging

    from core_api.constants import EVOLVE_MAX_RELATED_IDS
    from core_api.services.evolve_service import _adjust_weights

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    # All invalid UUIDs — we only care about the truncation log, not the updates
    oversized = [f"not-a-uuid-{i}" for i in range(EVOLVE_MAX_RELATED_IDS + 10)]

    with caplog.at_level(logging.WARNING, logger="core_api.services.evolve_service"):
        _, _, adjustments = await _adjust_weights(
            db, tenant_id, oversized, "success", "test-agent"
        )

    assert adjustments == []
    assert any("truncated" in rec.message for rec in caplog.records)


class TestConfidenceParsing:
    """_generate_rule must defend against LLMs returning non-numeric confidence."""

    def test_confidence_none_coerced_to_zero(self):
        """confidence=None from the LLM must not raise."""
        confidence_raw = None
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        assert confidence == 0.0

    def test_confidence_string_coerced_to_zero(self):
        """confidence='high' from the LLM must not raise."""
        confidence_raw = "high"
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        assert confidence == 0.0


@pytest.mark.asyncio
async def test_evolve_no_related_ids(db):
    """Reporting outcome with no related_ids → only outcome memory, no adjustments."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    from core_api.services.evolve_service import report_outcome

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"Something happened [{tag}]",
        outcome_type="success",
        related_ids=None,
        agent_id="test-agent",
    )

    assert result["outcome_id"] is not None
    assert result["outcome_type"] == "success"
    assert result["weight_adjustments"] == []
    assert result["rules_generated"] == []
    assert "evolve_ms" in result


@pytest.mark.asyncio
async def test_evolve_persists_outcome_memory(db):
    """Outcome is persisted as a memory of type 'outcome'."""
    from sqlalchemy import select

    from common.models.memory import Memory

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    from core_api.services.evolve_service import report_outcome

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"Test outcome [{tag}]",
        outcome_type="failure",
        related_ids=None,
        agent_id="test-agent",
    )
    assert result["outcome_id"] is not None

    # Query for the outcome memory
    stmt = select(Memory).where(
        Memory.tenant_id == tenant_id,
        Memory.memory_type == "outcome",
    )
    rows = (await db.execute(stmt)).scalars().all()
    assert len(rows) >= 1

    outcome_mem = rows[0]
    assert outcome_mem.memory_type == "outcome"
    assert tag in outcome_mem.content
    assert outcome_mem.metadata_ is not None
    assert outcome_mem.metadata_.get("outcome_type") == "failure"


@pytest.mark.asyncio
async def test_evolve_generate_rule_returns_valid_structure(db, sc):
    """_generate_rule with fake LLM returns a valid rule structure."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import _generate_rule
    from core_api.services.organization_settings import resolve_config

    # Audit P3 (evolve): ``_generate_rule`` no longer takes ``db`` —
    # callers resolve the tenant config first and pass it in. This
    # lets the MCP tool close its session before the LLM round-trip.
    config = await resolve_config(db, tenant_id)

    # A10: _generate_rule now returns (skip_reason, rule_dict) tuple.
    reason, rule = await _generate_rule(
        tenant_id=tenant_id,
        outcome=f"Failed because of bad info [{tag}]",
        outcome_type="failure",
        related_ids=[mid],
        config=config,
        agent_id="evolve-test-agent",
        fleet_id=None,
    )

    # FakeLLMProvider.complete_json returns {} — _generate_rule sanitizes to valid structure
    assert reason is None, f"unexpected skip reason: {reason}"
    assert rule is not None
    assert "condition" in rule
    assert "action" in rule
    assert "confidence" in rule
    assert "reasoning" in rule
    # Confidence is 0.0 with fake provider (empty dict from FakeLLMProvider)
    # This is below EVOLVE_RULE_CONFIDENCE_THRESHOLD so no rule would be persisted
    assert 0.0 <= rule["confidence"] <= 1.0


class TestFakeRuleFallback:
    """Test _fake_rule (used when ALL LLM providers fail, not when fake is primary)."""

    def test_fake_rule_has_sufficient_confidence(self):
        from core_api.constants import EVOLVE_RULE_CONFIDENCE_THRESHOLD
        from core_api.services.evolve_service import _fake_rule

        rule = _fake_rule()
        assert rule["confidence"] >= EVOLVE_RULE_CONFIDENCE_THRESHOLD


@pytest.mark.asyncio
async def test_evolve_failure_with_related_ids_adjusts_and_records(db, sc):
    """Failure with related_ids adjusts weights and persists outcome."""
    from sqlalchemy import select

    from common.models.memory import Memory

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import report_outcome

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"Failed because of bad info [{tag}]",
        outcome_type="failure",
        related_ids=[mid],
        agent_id="evolve-test-agent",
    )

    # Weight should have been adjusted
    assert len(result["weight_adjustments"]) == 1
    assert result["weight_adjustments"][0]["old_weight"] == 0.5
    assert result["outcome_id"] is not None
    assert result["outcome_type"] == "failure"

    # Outcome memory should exist
    stmt = select(Memory).where(
        Memory.tenant_id == tenant_id,
        Memory.memory_type == "outcome",
    )
    rows = (await db.execute(stmt)).scalars().all()
    assert len(rows) >= 1
    assert rows[0].metadata_.get("outcome_type") == "failure"


@pytest.mark.asyncio
async def test_evolve_success_no_rule(db, sc):
    """Success outcome does NOT generate a rule."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mid, _ = await _create_test_memory_via_sc(sc, tenant_id, weight=0.5)

    from core_api.services.evolve_service import report_outcome

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"Worked great [{tag}]",
        outcome_type="success",
        related_ids=[mid],
        agent_id="evolve-test-agent",
    )

    assert result["rules_generated"] == []


@pytest.mark.asyncio
async def test_evolve_response_shape(db):
    """Full response has all expected fields."""
    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    from core_api.services.evolve_service import report_outcome

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"Test shape [{tag}]",
        outcome_type="partial",
        related_ids=None,
        agent_id="test-agent",
    )

    assert "outcome_id" in result
    assert "outcome_type" in result
    assert "scope" in result
    assert "weight_adjustments" in result
    assert "rules_generated" in result
    assert "out_of_scope_count" in result
    assert "evolve_ms" in result
    assert result["scope"] == "agent"  # default
    assert result["out_of_scope_count"] == 0
    assert isinstance(result["weight_adjustments"], list)
    assert isinstance(result["rules_generated"], list)
    assert isinstance(result["evolve_ms"], int)


# ---------------------------------------------------------------------------
# Bug #3 — whitespace outcome rejected at the service layer
# ---------------------------------------------------------------------------


class TestWhitespaceOutcomeRejection:
    """report_outcome must reject whitespace-only outcomes.

    The MCP handler and REST endpoint validate this too, but the service
    is the authoritative gate so any caller path (direct invocation,
    future routes, tests) can't bypass it. The service raises ValueError
    so it stays decoupled from FastAPI — callers translate to the right
    HTTP status themselves.
    """

    @pytest.mark.asyncio
    async def test_empty_string_raises(self):
        from core_api.services.evolve_service import report_outcome

        with pytest.raises(ValueError) as exc_info:
            await report_outcome(None, "t1", outcome="", outcome_type="success")
        assert "non-empty" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_whitespace_only_raises(self):
        from core_api.services.evolve_service import report_outcome

        with pytest.raises(ValueError) as exc_info:
            await report_outcome(
                None, "t1", outcome="   \n\t  ", outcome_type="success"
            )
        assert "non-empty" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Bug #1 — scope validation + filtering + trust gating
# ---------------------------------------------------------------------------


class TestScopeValidation:
    """Invalid scope and missing fleet_id are rejected at the service layer
    via ValueError (decoupled from FastAPI)."""

    @pytest.mark.asyncio
    async def test_invalid_scope_raises(self):
        from core_api.services.evolve_service import report_outcome

        with pytest.raises(ValueError) as exc_info:
            await report_outcome(
                None, "t1", outcome="x", outcome_type="success", scope="bogus"
            )
        assert "scope" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_scope_fleet_without_fleet_id_raises(self):
        from core_api.services.evolve_service import report_outcome

        with pytest.raises(ValueError) as exc_info:
            await report_outcome(
                None,
                "t1",
                outcome="x",
                outcome_type="success",
                scope="fleet",
                fleet_id=None,
            )
        assert "fleet_id" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_filter_by_scope_agent_drops_other_agents_memories(db):
    """scope='agent' keeps only memories owned by the caller.

    Uses the direct-DB helper (_create_test_memory) rather than the
    storage-client path so the test doesn't go through the HTTP
    agent-upsert dance — it only needs rows visible to the ``db``
    session, which is exactly where ``_filter_by_scope`` queries.
    """
    from core_api.services.evolve_service import _filter_by_scope

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    mine_a, _ = await _create_test_memory(db, tenant_id, agent_id="agent-a")
    mine_b, _ = await _create_test_memory(db, tenant_id, agent_id="agent-a")
    other, _ = await _create_test_memory(db, tenant_id, agent_id="agent-b")

    kept, dropped = await _filter_by_scope(
        db,
        tenant_id=tenant_id,
        caller_agent_id="agent-a",
        fleet_id=None,
        scope="agent",
        related_ids=[mine_a, mine_b, other],
    )
    assert set(kept) == {mine_a, mine_b}
    assert dropped == 1


@pytest.mark.asyncio
async def test_filter_by_scope_fleet_drops_other_fleets(db):
    """scope='fleet' keeps only memories in the caller's fleet_id."""
    from core_api.services.evolve_service import _filter_by_scope

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    ours, _ = await _create_test_memory(db, tenant_id, agent_id="a", fleet_id="fleet-x")
    theirs, _ = await _create_test_memory(
        db, tenant_id, agent_id="b", fleet_id="fleet-y"
    )

    kept, dropped = await _filter_by_scope(
        db,
        tenant_id=tenant_id,
        caller_agent_id="a",
        fleet_id="fleet-x",
        scope="fleet",
        related_ids=[ours, theirs],
    )
    assert kept == [ours]
    assert dropped == 1


@pytest.mark.asyncio
async def test_filter_by_scope_all_keeps_everything_in_tenant(db):
    """scope='all' keeps any memory in the tenant regardless of owner/fleet."""
    from core_api.services.evolve_service import _filter_by_scope

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    m1, _ = await _create_test_memory(db, tenant_id, agent_id="a", fleet_id="fa")
    m2, _ = await _create_test_memory(db, tenant_id, agent_id="b", fleet_id="fb")

    kept, dropped = await _filter_by_scope(
        db,
        tenant_id=tenant_id,
        caller_agent_id="a",
        fleet_id=None,
        scope="all",
        related_ids=[m1, m2],
    )
    assert set(kept) == {m1, m2}
    assert dropped == 0


@pytest.mark.asyncio
async def test_filter_by_scope_drops_invalid_uuid_and_missing(db):
    """Non-parseable UUIDs and missing rows count toward out_of_scope_count."""
    from core_api.services.evolve_service import _filter_by_scope

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"
    mine, _ = await _create_test_memory(db, tenant_id, agent_id="a")

    kept, dropped = await _filter_by_scope(
        db,
        tenant_id=tenant_id,
        caller_agent_id="a",
        fleet_id=None,
        scope="agent",
        related_ids=[mine, "not-a-uuid", "00000000-0000-0000-0000-000000000000"],
    )
    assert kept == [mine]
    assert dropped == 2


@pytest.mark.asyncio
async def test_filter_by_scope_empty_input(db):
    """None / empty input returns ([], 0) without touching the DB."""
    from core_api.services.evolve_service import _filter_by_scope

    kept, dropped = await _filter_by_scope(
        db,
        tenant_id="t1",
        caller_agent_id="a",
        fleet_id=None,
        scope="agent",
        related_ids=[],
    )
    assert kept == []
    assert dropped == 0


@pytest.mark.asyncio
async def test_evolve_scope_agent_filters_out_other_agent_ids(db):
    """report_outcome with scope='agent' silently drops memories owned by
    other agents — only the caller's memories get weight-adjusted, and
    out_of_scope_count reflects the drop.

    Uses outcome_type='success' so the rule-generation path (which calls
    storage-api over HTTP) is not exercised — this test cares only about
    scope filtering + weight adjustment, both of which live on ``db``.
    """
    from core_api.services.evolve_service import report_outcome

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    mine, _ = await _create_test_memory(db, tenant_id, agent_id="caller-a", weight=0.5)
    other, _ = await _create_test_memory(db, tenant_id, agent_id="agent-b", weight=0.5)

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"agent-scope test [{tag}]",
        outcome_type="success",
        related_ids=[mine, other],
        scope="agent",
        agent_id="caller-a",
    )

    adjusted_ids = {a["memory_id"] for a in result["weight_adjustments"]}
    assert adjusted_ids == {mine}
    assert result["out_of_scope_count"] == 1
    assert result["scope"] == "agent"


@pytest.mark.asyncio
async def test_evolve_scope_all_adjusts_any_memory(db):
    """scope='all' lets the caller adjust memories they don't own (within tenant)."""
    from core_api.services.evolve_service import report_outcome

    tag = _uid()
    tenant_id = f"test-tenant-{tag}"

    m1, _ = await _create_test_memory(db, tenant_id, agent_id="caller")
    m2, _ = await _create_test_memory(db, tenant_id, agent_id="other")

    result = await report_outcome(
        db,
        tenant_id=tenant_id,
        outcome=f"all-scope test [{tag}]",
        outcome_type="success",
        related_ids=[m1, m2],
        scope="all",
        agent_id="caller",
    )

    adjusted_ids = {a["memory_id"] for a in result["weight_adjustments"]}
    assert adjusted_ids == {m1, m2}
    assert result["out_of_scope_count"] == 0


@pytest.mark.asyncio
async def test_evolve_outcome_memory_visibility_matches_scope(db, sc):
    """The persisted outcome memory's visibility follows _SCOPE_TO_VISIBILITY."""
    from sqlalchemy import select

    from common.models.memory import Memory
    from core_api.services.evolve_service import (
        _SCOPE_TO_VISIBILITY,
        report_outcome,
    )

    for scope in ("agent", "fleet", "all"):
        tag = _uid()
        tenant_id = f"test-tenant-{tag}"

        result = await report_outcome(
            db,
            tenant_id=tenant_id,
            outcome=f"vis test {scope} [{tag}]",
            outcome_type="success",
            related_ids=None,
            scope=scope,
            agent_id="test-agent",
            fleet_id="fleet-x" if scope == "fleet" else None,
        )

        stmt = select(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.memory_type == "outcome",
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 1, (
            f"scope={scope}: expected 1 outcome memory, got {len(rows)}"
        )
        assert rows[0].visibility == _SCOPE_TO_VISIBILITY[scope], (
            f"scope={scope}: visibility {rows[0].visibility} != {_SCOPE_TO_VISIBILITY[scope]}"
        )
        assert rows[0].metadata_.get("scope") == scope
        assert result["scope"] == scope


# ---------------------------------------------------------------------------
# MCP handler — trust gating
# ---------------------------------------------------------------------------


class TestMCPHandlerTrustGating:
    """memclaw_evolve handler must call _require_trust with scope-derived min_level."""

    @pytest.mark.asyncio
    async def test_scope_agent_requires_trust_1(self, monkeypatch, mcp_env):
        """scope='agent' requests trust ≥ 1 from _require_trust."""
        from unittest.mock import AsyncMock

        from core_api import mcp_server

        captured: dict = {}

        async def fake_require_trust(db, tenant_id, agent_id, min_level):
            captured["min_level"] = min_level
            return 3, False, None  # allow

        monkeypatch.setattr(mcp_server, "_require_trust", fake_require_trust)
        monkeypatch.setattr(
            mcp_server.services.evolve_service
            if hasattr(mcp_server, "services")
            else mcp_server,
            "report_outcome",
            AsyncMock(return_value={"outcome_id": "x"}),
            raising=False,
        )

        # The handler imports report_outcome lazily; patch at its origin module too.
        from core_api.services import evolve_service

        monkeypatch.setattr(
            evolve_service,
            "report_outcome",
            AsyncMock(return_value={"outcome_id": "x"}),
        )

        await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="success",
            scope="agent",
        )
        assert captured["min_level"] == 1

    @pytest.mark.asyncio
    async def test_scope_fleet_requires_trust_2(self, monkeypatch, mcp_env):
        """scope='fleet' escalates the required trust to 2."""
        from unittest.mock import AsyncMock

        from core_api import mcp_server
        from core_api.services import evolve_service

        captured: dict = {}

        async def fake_require_trust(db, tenant_id, agent_id, min_level):
            captured["min_level"] = min_level
            return 3, False, None

        monkeypatch.setattr(mcp_server, "_require_trust", fake_require_trust)
        monkeypatch.setattr(
            evolve_service,
            "report_outcome",
            AsyncMock(return_value={"outcome_id": "x"}),
        )

        await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="failure",
            scope="fleet",
            fleet_id="f1",
        )
        assert captured["min_level"] == 2

    @pytest.mark.asyncio
    async def test_scope_all_requires_trust_2(self, monkeypatch, mcp_env):
        """scope='all' also escalates to trust 2."""
        from unittest.mock import AsyncMock

        from core_api import mcp_server
        from core_api.services import evolve_service

        captured: dict = {}

        async def fake_require_trust(db, tenant_id, agent_id, min_level):
            captured["min_level"] = min_level
            return 3, False, None

        monkeypatch.setattr(mcp_server, "_require_trust", fake_require_trust)
        monkeypatch.setattr(
            evolve_service,
            "report_outcome",
            AsyncMock(return_value={"outcome_id": "x"}),
        )

        await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="success",
            scope="all",
        )
        assert captured["min_level"] == 2

    @pytest.mark.asyncio
    async def test_trust_denial_returns_403_envelope(self, monkeypatch, mcp_env):
        """When _require_trust returns an error, the handler surfaces it without
        running report_outcome."""
        from unittest.mock import AsyncMock

        from core_api import mcp_server
        from core_api.services import evolve_service

        async def fake_require_trust(db, tenant_id, agent_id, min_level):
            return 0, False, "Error (403): Agent 'x' (trust_level=0) < required 2."

        monkeypatch.setattr(mcp_server, "_require_trust", fake_require_trust)
        service_spy = AsyncMock(return_value={"outcome_id": "x"})
        monkeypatch.setattr(evolve_service, "report_outcome", service_spy)

        out = await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="failure",
            scope="all",
        )
        assert "FORBIDDEN" in as_text(out)
        assert service_spy.await_count == 0


class TestMCPHandlerScopeValidation:
    """Scope + fleet_id validation must happen before rate-limit / trust checks."""

    @pytest.mark.asyncio
    async def test_invalid_scope_rejected(self, mcp_env):
        from core_api import mcp_server

        out = await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="success",
            scope="bogus",
        )
        text = as_text(out)
        assert "INVALID_ARGUMENTS" in text
        assert "scope" in text.lower()

    @pytest.mark.asyncio
    async def test_scope_fleet_without_fleet_id_rejected(self, mcp_env):
        from core_api import mcp_server

        out = await mcp_server.memclaw_evolve(
            outcome="ok",
            outcome_type="failure",
            scope="fleet",
            fleet_id=None,
        )
        text = as_text(out)
        assert "INVALID_ARGUMENTS" in text
        assert "fleet_id" in text.lower()


# ---------------------------------------------------------------------------
# REST — EvolveRequest pydantic validator
# ---------------------------------------------------------------------------


class TestEvolveRequestValidation:
    """EvolveRequest.outcome must strip and require non-empty."""

    def test_whitespace_only_rejected_by_pydantic(self):
        from pydantic import ValidationError

        from core_api.routes.evolve import EvolveRequest

        with pytest.raises(ValidationError) as exc_info:
            EvolveRequest(
                tenant_id="t1",
                outcome="   \n  ",
                outcome_type="success",
            )
        # Detail can land in either the 'msg' or 'ctx' depending on pydantic;
        # look for the substring across the serialized errors.
        serialized = str(exc_info.value)
        assert "non-empty" in serialized.lower() or "non empty" in serialized.lower()

    def test_outcome_stripped_when_valid(self):
        from core_api.routes.evolve import EvolveRequest

        req = EvolveRequest(
            tenant_id="t1",
            outcome="  done  \n",
            outcome_type="success",
        )
        assert req.outcome == "done"


# ---------------------------------------------------------------------------
# REST identity contract — body.agent_id is the identity when no gateway-
# verified X-Agent-ID is present (parity with write/search endpoints).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolve_rest_rejects_unknown_agent(client):
    """A previously-unseen ``body.agent_id`` from a tenant-key caller
    must 403 on the WRITE path. The trust soft-pass closes a usability
    gap on read-only callers (memclaw_list, recall) but write paths
    persist memories + audit-log rows keyed to ``caller_agent_id``, so
    identity needs to be a real registered row before the audit trail
    can be trusted. Operators register agents by writing one memory
    first — see ``test_evolve_rest_accepts_registered_agent_with_trust``.
    """
    tenant_id = "default"
    resp = await client.post(
        "/api/v1/evolve/report",
        json={
            "tenant_id": tenant_id,
            "outcome": "first contact from new agent",
            "outcome_type": "success",
            "agent_id": f"ghost-agent-{uuid.uuid4().hex[:8]}",
            "scope": "agent",
        },
    )
    assert resp.status_code == 403
    assert "not registered" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_insights_rest_rejects_unknown_agent(client):
    """Mirror of ``test_evolve_rest_rejects_unknown_agent``. Insights
    REST persists insight memories + audit-log rows so the same
    identity-pinning rule applies — soft-pass is read-only territory."""
    tenant_id = "default"
    resp = await client.post(
        "/api/v1/insights/generate",
        json={
            "tenant_id": tenant_id,
            "focus": "patterns",
            "scope": "agent",
            "agent_id": f"ghost-agent-{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 403
    assert "not registered" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_evolve_rest_accepts_registered_agent_with_trust(client):
    """Happy path: a tenant-key caller naming an agent that exists in the
    tenant at default trust (≥ 1) clears scope='agent' without any gateway
    header. Parity with write/search.
    """
    tenant_id = "default"
    agent_id = f"evolve-agent-{uuid.uuid4().hex[:8]}"

    # Register the agent by writing one memory — auto-creates at DEFAULT_TRUST_LEVEL.
    write = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "content": f"seed memory {uuid.uuid4().hex}",
        },
    )
    assert write.status_code == 201, write.text

    resp = await client.post(
        "/api/v1/evolve/report",
        json={
            "tenant_id": tenant_id,
            "outcome": f"acted on memory {uuid.uuid4().hex}",
            "outcome_type": "success",
            "agent_id": agent_id,
            "scope": "agent",
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_insights_rest_accepts_registered_agent_with_trust(client):
    """Happy path for insights: registered agent at default trust clears
    scope='agent' with only a tenant key."""
    tenant_id = "default"
    agent_id = f"insights-agent-{uuid.uuid4().hex[:8]}"

    write = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "content": f"seed memory {uuid.uuid4().hex}",
        },
    )
    assert write.status_code == 201, write.text

    resp = await client.post(
        "/api/v1/insights/generate",
        json={
            "tenant_id": tenant_id,
            "focus": "patterns",
            "scope": "agent",
            "agent_id": agent_id,
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_evolve_rest_translates_service_valueerror_to_422(monkeypatch, client):
    """When report_outcome raises ValueError (the service's FastAPI-free
    defensive gate), the route translates it to HTTPException(422)."""
    from tests.conftest import get_admin_headers

    async def _raise(*args, **kwargs):
        raise ValueError("synthetic service validation error")

    # The route lazy-imports report_outcome each request, so patching the
    # source module rebinds the name for every incoming request.
    from core_api.services import evolve_service

    monkeypatch.setattr(evolve_service, "report_outcome", _raise)

    resp = await client.post(
        "/api/v1/evolve/report",
        json={
            "tenant_id": "default",
            "outcome": "anything",
            "outcome_type": "success",
        },
        headers=get_admin_headers(),
    )
    assert resp.status_code == 422
    assert "synthetic" in resp.json()["detail"]
