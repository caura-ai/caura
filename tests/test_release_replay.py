"""A released write gets the work its hold skipped (g2.8).

``replay_released_write`` reads the released row and decides from it what never
ran: enrichment for a fast write that the background enrichment skipped, the
governance verdict and the facts kept on the row for one that was enriched,
then the near-duplicate merge, entity extraction and contradiction detection.
Each collaborator is patched where the replay imports it from, so these pin
what it calls and with what, not what those calls do.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.services import release_replay
from core_api.services.governance_remediation import (
    GovernanceCascadeError,
    RemediationOutcome,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_EMBEDDING = [0.1] * 4


def _row(*, system: dict | None = None, top: dict | None = None, **fields) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": "t",
        "fleet_id": "f1",
        "agent_id": "low",
        "memory_type": "fact",
        "content": "the deploy window moved to thursday",
        "title": None,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "status": "active",
        "visibility": "scope_team",
        "embedding": _EMBEDDING,
        "metadata_": {**(top or {}), "_system": dict(system or {})},
    }
    row.update(fields)
    return row


@pytest.fixture
def calls(monkeypatch):
    found = {
        "get_memory": AsyncMock(),
        "enrich": AsyncMock(),
        "remediate": AsyncMock(return_value=RemediationOutcome()),
        "fan_out": AsyncMock(),
        "merge": AsyncMock(),
        "extract": AsyncMock(),
        "contradict": AsyncMock(),
        "record": AsyncMock(),
    }
    monkeypatch.setattr(
        release_replay,
        "get_storage_client",
        lambda: SimpleNamespace(get_memory=found["get_memory"]),
    )
    monkeypatch.setattr(release_replay, "record_task_failure", found["record"])
    config = SimpleNamespace(entity_extraction_enabled=True)
    found["config"] = config
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=config),
    )
    monkeypatch.setattr(
        "core_api.services.memory_service._schedule_enrich_or_inline", found["enrich"]
    )
    monkeypatch.setattr(
        "core_api.services.governance_remediation.remediate_after_enrichment",
        found["remediate"],
    )
    monkeypatch.setattr(
        "core_api.consumer._fan_out_persisted_atomic_facts", found["fan_out"]
    )
    monkeypatch.setattr(
        "core_api.pipeline.steps.write.schedule_background_tasks._merge_near_duplicate",
        found["merge"],
    )
    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction",
        found["extract"],
    )
    monkeypatch.setattr(
        "core_api.services.contradiction.run_contradiction_detection",
        found["contradict"],
    )
    return found


async def _replay(calls, row: dict | None) -> None:
    calls["get_memory"].return_value = row
    await release_replay.replay_released_write(
        row["id"] if row else str(uuid.uuid4()), "t"
    )


async def test_a_never_enriched_write_is_enriched_now_and_governed(calls):
    """A fast write whose background enrichment read it back while it was held."""
    row = _row(
        system={
            "write_mode": "fast",
            "enrichment_pending": True,
            "memory_type_agent_set": True,
            "weight_source": "default",
        },
        title="the writer's own title",
        top={"summary": "mine"},
    )
    row["metadata_"]["_system"]["caller_owned"] = ["summary"]

    await _replay(calls, row)

    kwargs = calls["enrich"].await_args.kwargs
    assert kwargs["run_governance_remediation"] is True
    # The type the writer chose and the title it set stay; the default weight
    # is the enrichment's to fill.
    assert kwargs["agent_provided_fields"] == ["memory_type", "status", "title"]
    assert kwargs["caller_owned_metadata_keys"] == ["summary"]
    # Enrichment governs and fans out itself.
    calls["remediate"].assert_not_awaited()
    calls["fan_out"].assert_not_awaited()
    calls["extract"].assert_awaited_once()
    calls["contradict"].assert_awaited_once()


async def test_a_fast_write_enriched_while_held_is_governed_then_fanned_out(calls):
    """The worker enriched it and kept its facts; the ENRICHED consumer skipped it."""
    row = _row(system={"write_mode": "fast"})
    calls["remediate"].return_value = RemediationOutcome(visibility="scope_agent")

    await _replay(calls, row)

    calls["enrich"].assert_not_awaited()
    calls["remediate"].assert_awaited_once()
    _sc, memory, payload, outcome = calls["fan_out"].await_args.args
    assert memory is row
    assert (str(payload.memory_id), payload.tenant_id) == (row["id"], "t")
    # The children inherit what governance made of the parent (#808).
    assert outcome.visibility == "scope_agent"
    calls["extract"].assert_awaited_once()
    calls["contradict"].assert_awaited_once()


async def test_a_strong_write_is_not_governed_twice(calls):
    """``GovernanceDecision`` governed it before it was written."""
    await _replay(calls, _row(system={"write_mode": "strong"}))

    calls["remediate"].assert_not_awaited()
    assert calls["fan_out"].await_args.args[3].visibility is None


async def test_a_bulk_write_is_governed(calls):
    """Bulk records no write mode and never runs ``GovernanceDecision``."""
    await _replay(calls, _row(system={}))

    calls["remediate"].assert_awaited_once()


async def test_a_write_governance_drops_gets_nothing_more(calls):
    calls["remediate"].return_value = RemediationOutcome(dropped=True)

    await _replay(
        calls,
        _row(
            system={
                "write_mode": "fast",
                "near_duplicate_of": str(uuid.uuid4()),
                "near_duplicate_merged": True,
            }
        ),
    )

    for name in ("fan_out", "merge", "extract", "contradict"):
        calls[name].assert_not_awaited()


async def test_the_merge_it_meant_to_make_is_made_now(calls):
    candidate = str(uuid.uuid4())
    row = _row(
        system={
            "write_mode": "fast",
            "near_duplicate_of": candidate,
            "near_duplicate_merged": True,
        }
    )

    await _replay(calls, row)

    assert calls["merge"].await_args.args == (row["id"], candidate, "t")


async def test_a_near_duplicate_it_did_not_mean_to_merge_stays(calls):
    """Fast mode records the nearest row whether or not it decided to merge."""
    await _replay(
        calls,
        _row(system={"write_mode": "fast", "near_duplicate_of": str(uuid.uuid4())}),
    )

    calls["merge"].assert_not_awaited()


async def test_without_an_embedding_contradictions_wait_for_it(calls):
    """The EMBEDDED back-channel runs them when the vector lands."""
    await _replay(calls, _row(system={"write_mode": "fast"}, embedding=None))

    calls["contradict"].assert_not_awaited()
    calls["extract"].assert_awaited_once()


async def test_entity_extraction_follows_the_tenant_switch(calls):
    calls["config"].entity_extraction_enabled = False

    await _replay(calls, _row(system={"write_mode": "fast"}))

    calls["extract"].assert_not_awaited()
    calls["contradict"].assert_awaited_once()


def _recorded(calls, row: dict) -> str:
    """What the one failure recorded says; the replay's wrapper never sees it."""
    task, memory_id, tenant_id, exc = calls["record"].await_args.args
    assert (task, str(memory_id), tenant_id) == ("release_replay", row["id"], "t")
    return str(exc)


async def test_a_failing_step_is_recorded_and_does_not_stop_the_rest(calls):
    calls["extract"].side_effect = RuntimeError("extraction provider down")
    row = _row(system={"write_mode": "fast"})

    await _replay(calls, row)

    calls["contradict"].assert_awaited_once()
    assert _recorded(calls, row) == (
        "entity extraction failed: RuntimeError: extraction provider down"
    )


async def test_a_failed_enrichment_is_recorded_and_the_rest_still_run(calls):
    """A publish that fails, or a cascade enrichment leaves for its wrapper."""
    calls["enrich"].side_effect = RuntimeError("bus down")
    row = _row(system={"write_mode": "fast", "enrichment_pending": True})

    await _replay(calls, row)

    assert _recorded(calls, row).startswith("enrichment failed")
    calls["extract"].assert_awaited_once()
    calls["contradict"].assert_awaited_once()


async def test_a_verdict_that_failed_makes_no_children(calls):
    """Fail closed: its facts stay on the row, for the recorded failure."""
    calls["remediate"].side_effect = RuntimeError("storage down")
    row = _row(system={"write_mode": "fast"})

    await _replay(calls, row)

    calls["fan_out"].assert_not_awaited()
    assert _recorded(calls, row).startswith("governance remediation failed")
    calls["extract"].assert_awaited_once()
    calls["contradict"].assert_awaited_once()


@pytest.mark.parametrize("dropped", [True, False])
async def test_derived_rows_left_unremediated_stop_the_fan_out(calls, dropped):
    """``remediate_after_enrichment`` asks a caller about to derive more rows to
    stop when it couldn't clean up the ones already derived. A drop that applied
    to the row itself still ends the replay."""
    outcome = RemediationOutcome(dropped=dropped, visibility="scope_agent")
    calls["remediate"].side_effect = GovernanceCascadeError("a child", outcome)
    row = _row(system={"write_mode": "fast"})

    await _replay(calls, row)

    calls["fan_out"].assert_not_awaited()
    assert _recorded(calls, row).startswith("governance cleanup of derived rows")
    assert calls["extract"].await_count == int(not dropped)
    assert calls["contradict"].await_count == int(not dropped)


async def test_a_write_deleted_since_its_release_gets_nothing(calls):
    await _replay(calls, None)

    for name in ("enrich", "remediate", "fan_out", "merge", "extract", "contradict"):
        calls[name].assert_not_awaited()


@pytest.mark.parametrize(
    ("system", "fields", "pins"),
    [
        ({}, {}, ["memory_type", "status", "weight"]),
        ({"memory_type_agent_set": False, "weight_source": "llm"}, {}, ["status"]),
        (
            {"memory_type_agent_set": True, "weight_source": "caller"},
            {},
            ["memory_type", "status", "weight"],
        ),
        (
            {"memory_type_agent_set": False, "weight_source": "default"},
            {"title": "t", "ts_valid_start": "2026-10-07T00:00:00+00:00"},
            ["status", "title", "ts_valid_start"],
        ),
    ],
)
async def test_a_released_write_keeps_what_its_writer_set(system, fields, pins):
    """A row that doesn't say who chose its type or weight keeps both."""
    assert release_replay.release_pins(_row(**fields), system) == pins


async def test_a_caller_can_not_decide_the_merge_a_release_makes():
    """A release merges into ``near_duplicate_of`` when the write decided to, so
    the decision is the platform's to record, like the candidate itself."""
    from core_api.services.system_metadata import sanitize_caller_metadata

    assert sanitize_caller_metadata(
        {"near_duplicate_merged": True, "near_duplicate_of": "x", "mine": 1}
    ) == {"mine": 1}
