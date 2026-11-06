"""Enrich again the memories whose enrichment fell back to the heuristic.

``replay_enrichment`` reads each listed row, decides from it whether a replay
applies, and with ``--apply`` schedules enrichment as a fast write does. The
storage client and ``_schedule_enrich_or_inline`` are patched where the command
imports them from, so these pin what it calls and with what.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.enrichment.service import fake_enrich
from core_api.maintenance import replay_enrichment as re_

pytestmark = pytest.mark.unit

_CONTENT = "- deployed v2.3 to production\n- fix the login bug\n- the team prefers short updates"
_CREATED = "2026-10-20T09:30:00+00:00"


def _row(*, system: dict | None = None, top: dict | None = None, **fields) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": "t",
        "fleet_id": "f1",
        "agent_id": "a1",
        "memory_type": "episode",
        "weight": 0.65,
        "content": _CONTENT,
        # What the heuristic left: its title, no dates.
        "title": fake_enrich(_CONTENT).title,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "status": "active",
        "created_at": _CREATED,
        "metadata_": {
            **(top or {}),
            "_system": {
                "memory_type_agent_set": False,
                "weight_source": "default",
                **(system or {}),
            },
        },
    }
    row.update(fields)
    return row


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_targets_load_in_file_order_without_duplicates(tmp_path):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    path = tmp_path / "ids.json"
    path.write_text(
        json.dumps(
            [
                {"tenant_id": "t1", "memory_id": a},
                {"tenant_id": "t2", "memory_id": b},
                {"tenant_id": "t1", "memory_id": a},
            ]
        )
    )
    assert re_.load_targets(path) == [re_.Target("t1", a), re_.Target("t2", b)]


@pytest.mark.parametrize(
    "body",
    [
        {"tenant_id": "t", "memory_id": str(uuid.uuid4())},
        [{"memory_id": str(uuid.uuid4())}],
        [{"tenant_id": "", "memory_id": str(uuid.uuid4())}],
        [{"tenant_id": "t", "memory_id": "not-a-uuid"}],
        ["not an object"],
    ],
    ids=["not an array", "no tenant", "empty tenant", "bad uuid", "not an object"],
)
def test_a_malformed_list_is_refused_whole(tmp_path, body):
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        re_.load_targets(path)


# ---------------------------------------------------------------------------
# What stays set
# ---------------------------------------------------------------------------


def test_the_heuristics_title_type_and_weight_are_replaced():
    row = _row()
    assert re_.replay_pins(row, row["metadata_"]["_system"]) == ["status"]


def test_a_title_set_after_the_heuristic_is_kept():
    row = _row(title="Release checklist")
    assert "title" in re_.replay_pins(row, row["metadata_"]["_system"])


def test_dates_on_the_row_are_kept():
    # The heuristic never sets a date, so one on the row is the writer's.
    row = _row(ts_valid_start="2026-11-01T00:00:00+00:00")
    assert "ts_valid_start" in re_.replay_pins(row, row["metadata_"]["_system"])


def test_what_the_writer_chose_is_kept():
    row = _row(system={"memory_type_agent_set": True, "weight_source": "caller"})
    pins = re_.replay_pins(row, row["metadata_"]["_system"])
    assert {"memory_type", "weight"} <= set(pins)


# ---------------------------------------------------------------------------
# Which rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory", "outcome"),
    [
        (None, re_.MISSING),
        (_row(status="archived"), re_.NOT_LIVE),
        (_row(status="deleted"), re_.NOT_LIVE),
        (_row(system={"enrichment_pending": True}), re_.PENDING),
        (_row(system={"llm_ms": 912}), re_.ENRICHED),
        (_row(content="   "), re_.NO_CONTENT),
        (_row(), re_.REPLAY),
        (_row(status="pending"), re_.REPLAY),
    ],
    ids=[
        "missing",
        "archived",
        "deleted",
        "pending",
        "enriched since",
        "no content",
        "replay",
        "live pending",
    ],
)
def test_each_row_gets_one_outcome(memory, outcome):
    system = re_.extract_system_metadata((memory or {}).get("metadata_")) or {}
    assert re_.classify(memory, system) == outcome


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@pytest.fixture
def run(monkeypatch):
    rows: dict[str, dict | None] = {}
    # Marks and enrichments, in the order they happened.
    events: list[tuple] = []
    get_memory = AsyncMock(
        side_effect=lambda memory_id, tenant_id, **_kw: rows[memory_id]
    )

    async def mark(memory_id, tenant_id, data):
        """Storage's merge: top level, then ``_system`` one level deep."""
        patch = data["metadata_patch"]
        assert patch["_system"] == {"enrichment_pending": patch["enrichment_pending"]}
        metadata = rows[memory_id]["metadata_"]
        metadata["enrichment_pending"] = patch["enrichment_pending"]
        metadata["_system"].update(patch["_system"])
        events.append(("pending", memory_id, patch["enrichment_pending"]))
        return {}

    update_memory = AsyncMock(side_effect=mark)
    enrich = AsyncMock(
        side_effect=lambda memory_id, *_a, **_kw: events.append(
            ("enrich", str(memory_id))
        )
    )
    config = SimpleNamespace(enrichment_provider="openai")
    sleep = AsyncMock()
    monkeypatch.setattr(
        re_,
        "get_storage_client",
        lambda: SimpleNamespace(get_memory=get_memory, update_memory=update_memory),
    )
    monkeypatch.setattr(re_, "_publishes_to_worker", lambda: True)
    monkeypatch.setattr(
        "core_api.services.memory_service._schedule_enrich_or_inline", enrich
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=config),
    )
    monkeypatch.setattr(re_.asyncio, "sleep", sleep)
    return SimpleNamespace(
        rows=rows,
        events=events,
        get_memory=get_memory,
        update_memory=update_memory,
        enrich=enrich,
        config=config,
        sleep=sleep,
    )


def _target(run, memory: dict | None) -> re_.Target:
    memory_id = (memory or {}).get("id") or str(uuid.uuid4())
    run.rows[memory_id] = memory
    return re_.Target("t", memory_id)


async def test_a_dry_run_reads_every_row_and_enriches_none(run, capsys):
    targets = [
        _target(run, _row()),
        _target(run, None),
        _target(run, _row(status="archived")),
    ]

    outcomes = await re_.replay(targets, apply=False, per_second=1.0)

    assert run.get_memory.await_count == 3
    run.enrich.assert_not_awaited()
    run.update_memory.assert_not_awaited()
    assert outcomes == {re_.REPLAY: 1, re_.MISSING: 1, re_.NOT_LIVE: 1}
    assert _CONTENT not in capsys.readouterr().out


async def test_apply_enriches_each_replayable_row_as_a_fast_write(run):
    row = _row(top={"summary": "mine"}, system={"caller_owned": ["summary"]})
    skipped = _row(system={"llm_ms": 800})

    outcomes = await re_.replay(
        [_target(run, row), _target(run, skipped)], apply=True, per_second=2.0
    )

    assert outcomes == {re_.REPLAY: 1, re_.ENRICHED: 1}
    run.enrich.assert_awaited_once()
    args, kwargs = run.enrich.await_args
    assert args == (uuid.UUID(row["id"]), _CONTENT, "t", "f1", "a1", run.config)
    assert kwargs == {
        "agent_provided_fields": ["status"],
        "caller_owned_metadata_keys": ["summary"],
        "reference_datetime": datetime.fromisoformat(_CREATED),
        "run_governance_remediation": True,
    }
    run.sleep.assert_awaited_once_with(0.5)
    # Marked before the request leaves, so a rerun while the worker is behind
    # reads the row as pending. Marked after, the worker's clear could land
    # first and leave it pending for good.
    assert run.events == [("pending", row["id"], True), ("enrich", row["id"])]


async def test_a_rerun_skips_rows_still_waiting_for_the_worker(run):
    row = _row()
    target = _target(run, row)

    await re_.replay([target], apply=True, per_second=1.0)
    # The worker has not landed yet, so the row still carries the mark.
    outcomes = await re_.replay([target], apply=True, per_second=1.0)

    assert outcomes == {re_.PENDING: 1}
    run.enrich.assert_awaited_once()


async def test_a_request_that_never_left_clears_its_mark(run):
    run.enrich.side_effect = RuntimeError("publish failed")
    row = _row()

    outcomes = await re_.replay([_target(run, row)], apply=True, per_second=1.0)

    assert outcomes == {re_.ERROR: 1}
    assert run.events == [("pending", row["id"], True), ("pending", row["id"], False)]


async def test_a_refused_mark_sends_nothing(run):
    run.update_memory.side_effect = None
    run.update_memory.return_value = None

    outcomes = await re_.replay([_target(run, _row())], apply=True, per_second=1.0)

    assert outcomes == {re_.ERROR: 1}
    run.enrich.assert_not_awaited()
    run.update_memory.assert_awaited_once()


async def test_inline_enrichment_needs_no_mark(run, monkeypatch):
    monkeypatch.setattr(re_, "_publishes_to_worker", lambda: False)
    row = _row()

    await re_.replay([_target(run, row)], apply=True, per_second=1.0)

    run.update_memory.assert_not_awaited()
    assert run.events == [("enrich", row["id"])]


async def test_apply_stops_after_five_failures_in_a_row(run):
    run.enrich.side_effect = RuntimeError("publish failed")
    targets = [_target(run, _row()) for _ in range(8)]

    outcomes = await re_.replay(targets, apply=True, per_second=1.0)

    assert run.enrich.await_count == 5
    assert outcomes == {re_.ERROR: 5}


async def test_a_row_that_cannot_be_read_is_an_error_not_a_stop(run):
    run.get_memory.side_effect = [RuntimeError("storage 503"), _row()]

    outcomes = await re_.replay(
        [re_.Target("t", str(uuid.uuid4())) for _ in range(2)],
        apply=False,
        per_second=1.0,
    )

    assert outcomes == {re_.ERROR: 1, re_.REPLAY: 1}


def test_the_rate_must_be_positive(tmp_path):
    with pytest.raises(SystemExit):
        re_.main([str(tmp_path / "ids.json"), "--per-second", "0"])
