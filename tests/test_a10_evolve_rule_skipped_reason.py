"""A10 — rule synthesis silent-skip observability.

Gap measured: ``rules_synthesized = 0`` end-to-end. The harness
instrument was fixed, but ``evolve_service`` has five silent-exit
paths where rule synthesis short-circuits without saying why:

  1. ``outcome_type != failure|partial`` OR ``not related_ids``
  2. all related memories failed to fetch
  3. LLM returned a non-dict response
  4. LLM raised
  5. confidence below ``EVOLVE_RULE_CONFIDENCE_THRESHOLD``
  6. ``_persist_rule`` raised

Each path silently returns ``rules_generated=[]`` with no signal for
operators. A10 makes every skip path:

  - **Log a structured line** of the form
    ``evolve_rule_skipped reason=<slug> tenant_id=<tid> outcome_id=<oid>``
    (matches the Gap-06 always-fire completion-log pattern from
    contradiction_detector).
  - **Populate a new ``rule_skipped_reason`` field** on the
    ``report_outcome`` response so callers / the harness can read the
    skip cause without parsing logs.

The actual rule-generation logic is unchanged — this PR is pure
observability. Once visible, the root cause of zero rules in any
given run can be localised in seconds (currently requires bisecting
the worker path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants — the exact reason slugs that callers may pattern-match on.
# ---------------------------------------------------------------------------


def test_rule_skip_reason_constants_defined():
    """A central enum of skip reasons so callers can pattern-match
    instead of grep'ing log strings."""
    from core_api.services.evolve_service import RULE_SKIP_REASONS

    # All six known silent-exit paths plus None-for-success.
    for slug in (
        "not_failure_or_partial",
        "no_related_ids",
        "no_memories_fetched",
        "llm_failed",
        "below_confidence_threshold",
        "persist_failed",
    ):
        assert slug in RULE_SKIP_REASONS


# ---------------------------------------------------------------------------
# Service contract — ``report_outcome`` returns rule_skipped_reason.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_includes_rule_skipped_reason_field():
    """Every call to ``report_outcome`` returns a ``rule_skipped_reason``
    key. None means "rule was generated"; a slug means "skipped because"."""
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
        result = await report_outcome(
            db,
            tenant_id="t1",
            outcome="passing test",
            outcome_type="success",  # success → not_failure_or_partial
            related_ids=None,
            agent_id="a1",
        )

    assert "rule_skipped_reason" in result


# ---------------------------------------------------------------------------
# Per-path reason coverage.
# ---------------------------------------------------------------------------


async def _run(outcome_type, related_ids=None, **patches):
    """Invoke ``report_outcome`` with sensible-default mocks; allow tests
    to override any internal via ``patches``."""
    from core_api.services.evolve_service import report_outcome

    db = AsyncMock()
    base_patches = {
        "_filter_by_scope": AsyncMock(return_value=(related_ids or [], 0)),
        "_adjust_weights": AsyncMock(return_value=(None, [], [])),
        "_persist_outcome": AsyncMock(
            return_value="00000000-0000-0000-0000-000000000001"
        ),
    }
    base_patches.update(patches)
    cm = []
    for name, mock in base_patches.items():
        cm.append(patch(f"core_api.services.evolve_service.{name}", new=mock))
    with patch.multiple(
        "core_api.services.evolve_service",
        **{k: v for k, v in base_patches.items()},
    ):
        return await report_outcome(
            db,
            tenant_id="t1",
            outcome="report",
            outcome_type=outcome_type,
            related_ids=related_ids,
            agent_id="a1",
        )


@pytest.mark.asyncio
async def test_skip_reason_not_failure_or_partial_on_success():
    """``success`` outcomes never generate rules. Report the reason
    explicitly so a curious operator can confirm it's by design."""
    result = await _run("success")
    assert result["rule_skipped_reason"] == "not_failure_or_partial"
    assert result["rules_generated"] == []


@pytest.mark.asyncio
async def test_skip_reason_no_related_ids_on_failure_without_ids():
    """``failure`` with empty ``related_ids`` — gate at evolve_service:633."""
    result = await _run("failure", related_ids=None)
    assert result["rule_skipped_reason"] == "no_related_ids"


@pytest.mark.asyncio
async def test_skip_reason_no_related_ids_on_partial_without_ids():
    result = await _run("partial", related_ids=None)
    assert result["rule_skipped_reason"] == "no_related_ids"


@pytest.mark.asyncio
async def test_skip_reason_no_memories_fetched_when_all_fetch_fail():
    """All ``sc.get_memory_for_tenant`` calls failed → ``_generate_rule``
    returns ``None`` with reason."""
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000aaa"],
        _generate_rule=AsyncMock(return_value=("no_memories_fetched", None)),
    )
    assert result["rule_skipped_reason"] == "no_memories_fetched"
    assert result["rules_generated"] == []


@pytest.mark.asyncio
async def test_skip_reason_llm_failed_on_non_dict_response():
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000aaa"],
        _generate_rule=AsyncMock(return_value=("llm_failed", None)),
    )
    assert result["rule_skipped_reason"] == "llm_failed"


@pytest.mark.asyncio
async def test_skip_reason_below_confidence_threshold():
    """Judge returned valid rule but ``confidence < 0.5``. Currently
    silently drops; A10 surfaces it."""
    rule_low_conf = {
        "condition": "if X",
        "action": "do Y",
        "confidence": 0.3,
        "reasoning": "...",
    }
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000aaa"],
        _generate_rule=AsyncMock(return_value=(None, rule_low_conf)),
    )
    assert result["rule_skipped_reason"] == "below_confidence_threshold"


@pytest.mark.asyncio
async def test_skip_reason_persist_failed_when_persist_returns_none():
    rule_ok = {
        "condition": "if X",
        "action": "do Y",
        "confidence": 0.9,
        "reasoning": "...",
    }
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000aaa"],
        _generate_rule=AsyncMock(return_value=(None, rule_ok)),
        _persist_rule=AsyncMock(return_value=None),
    )
    assert result["rule_skipped_reason"] == "persist_failed"


@pytest.mark.asyncio
async def test_skip_reason_none_on_successful_rule_generation():
    """Happy path: rule generated, persisted. ``rule_skipped_reason`` is
    None and ``rules_generated`` has one entry."""
    rule_ok = {
        "condition": "if X",
        "action": "do Y",
        "confidence": 0.9,
        "reasoning": "...",
    }
    result = await _run(
        "failure",
        related_ids=["00000000-0000-0000-0000-000000000aaa"],
        _generate_rule=AsyncMock(return_value=(None, rule_ok)),
        _persist_rule=AsyncMock(return_value="00000000-0000-0000-0000-000000000bbb"),
    )
    assert result["rule_skipped_reason"] is None
    assert len(result["rules_generated"]) == 1


# ---------------------------------------------------------------------------
# Structured log assertions — operators can grep ``evolve_rule_skipped``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_log_emitted_on_skip(caplog):
    """A skip emits one ``evolve_rule_skipped`` log line carrying the
    reason slug + tenant_id. Mirrors the ``path_a_completed`` /
    ``path_c_completed`` always-fire log pattern."""
    import logging

    caplog.set_level(logging.INFO, logger="core_api.services.evolve_service")
    await _run("success")  # not_failure_or_partial
    matching = [r for r in caplog.records if "evolve_rule_skipped" in r.message]
    assert len(matching) == 1, (
        f"expected one skip log; got {[r.message for r in caplog.records]}"
    )
    assert "not_failure_or_partial" in matching[0].message
