"""Rows the platform derives keep the trust of the rows they came from.

The crystallizer, evolve and insights write rows of their own built from other
agents' memories. Such a row must not read as more trusted than its weakest
source:

- a crystal records its least-trusted source's level (``source_trust``), is not
  built where the organization holds writes at that level for review, never
  retires a source trusted above it, and is not rebuilt beside the sources it
  kept;
- an evolve rule records the lowest level among its reporter and its sources;
- an insight records the lowest level among its requester and every row its
  run read, and a scope='all' insight is saved in the narrowest home those rows
  allow instead of org-wide. The ids a finding cites are the model's choice, so
  they widen neither;
- callers cannot write ``source_trust`` themselves.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.unit]

# Authors' trust levels, as storage's agents rows hold them.
_TRUST = {"lead": 3, "high": 2, "low": 1}


async def _get_agent(agent_id, tenant_id, read=True):
    level = _TRUST.get(agent_id)
    return None if level is None else {"agent_id": agent_id, "trust_level": level}


# ---------------------------------------------------------------------------
# Crystallizer
# ---------------------------------------------------------------------------


def _source(agent_id: str, **extra) -> dict:
    return {
        "id": str(uuid4()),
        "content": f"The release train runs weekly ({agent_id}).",
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
        "fleet_id": "f1",
        "agent_id": agent_id,
        **extra,
    }


async def _crystallize(sources: list[dict], *, hold_below: int = 0):
    """Run one crystallization over ``sources`` as one cluster."""
    from core_api.services.crystallizer_service import _run_crystallization

    by_id = {row["id"]: row for row in sources}
    pairs = [{"id1": a, "id2": b} for a, b in itertools.combinations(by_id, 2)]
    sc = AsyncMock()
    sc.bulk_get_memories = AsyncMock(
        side_effect=lambda ids, tenant_id: [by_id.get(i) for i in ids]
    )
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    sc.get_agent = AsyncMock(side_effect=_get_agent)
    config = SimpleNamespace(quarantine_below_trust=lambda _fleet_id: hold_below)
    distil = AsyncMock(
        return_value=[{"content": "Release train: weekly.", "memory_type": "fact"}]
    )
    created = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client", return_value=sc
        ),
        patch("core_api.clients.storage_client.get_storage_client", return_value=sc),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=config),
        ),
        patch("core_api.services.crystallizer_service._crystallize_cluster", distil),
        patch("core_api.services.memory_service.create_memory", created),
    ):
        await _run_crystallization(
            tenant_id="t1",
            fleet_id=None,
            hygiene={"near_duplicates": {"pairs": pairs}},
        )
    archived = {
        update["memory_id"]
        for call in sc.batch_update_status.await_args_list
        for update in call.args[0]["updates"]
    }
    return distil, created, archived


async def test_a_crystal_records_its_lowest_source_trust_and_keeps_higher_sources():
    low, high_a, high_b = _source("low"), _source("high"), _source("high")
    _distil, created, archived = await _crystallize([low, high_a, high_b])

    created.assert_awaited_once()
    assert created.await_args.kwargs.get("system_values") == {"source_trust": 1}
    # Only the source trusted no higher than the crystal is retired.
    assert archived == {low["id"]}


async def test_a_crystal_of_equally_trusted_sources_retires_them_all():
    sources = [_source("high") for _ in range(3)]
    _distil, created, archived = await _crystallize(sources)

    assert created.await_args.kwargs.get("system_values") == {"source_trust": 2}
    assert archived == {row["id"] for row in sources}


async def test_a_cluster_with_a_source_below_the_hold_level_is_left_alone():
    sources = [_source("low"), _source("high"), _source("high")]
    distil, created, archived = await _crystallize(sources, hold_below=2)

    distil.assert_not_awaited()
    created.assert_not_awaited()
    assert archived == set()


async def test_an_earlier_crystal_counts_at_the_trust_it_recorded():
    """Its synthetic author has no agents row; the recorded level stands in."""
    earlier = _source(
        "crystallizer",
        is_inferred=True,
        metadata_={"_system": {"source_trust": 2}},
    )
    sources = [earlier, _source("high"), _source("high")]
    _distil, created, archived = await _crystallize(sources)

    assert created.await_args.kwargs.get("system_values") == {"source_trust": 2}
    assert archived == {row["id"] for row in sources}


async def test_a_crystal_is_not_rebuilt_beside_the_sources_it_kept():
    """The next sweep finds the crystal and the sources it kept as one cluster.
    The crystal is all a new one could retire, so the cluster is left alone
    rather than crystallized again on every run."""
    low, high_a, high_b = _source("low"), _source("high"), _source("high")
    _distil, created, _archived = await _crystallize([low, high_a, high_b])
    crystal = _source(
        "crystallizer",
        is_inferred=True,
        metadata_={"_system": created.await_args.kwargs["system_values"]},
    )

    distil, created, archived = await _crystallize([crystal, high_a, high_b])

    distil.assert_not_awaited()
    created.assert_not_awaited()
    assert archived == set()


# ---------------------------------------------------------------------------
# Evolve
# ---------------------------------------------------------------------------


async def test_an_evolve_rule_records_the_lowest_trust_of_reporter_and_sources():
    from core_api.services.evolve_service import _generate_rule, _persist_rule

    related = {
        "title": "deploys",
        "content": "Deploys need one approval.",
        "weight": 0.5,
        "agent_id": "low",
    }
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=related)
    sc.get_agent = AsyncMock(side_effect=_get_agent)

    async def complete_json(prompt: str, **_kw):
        return {"condition": "c", "action": "a", "confidence": 0.9, "reasoning": "r"}

    async def fallback(*_args, call_fn, **_kw):
        return await call_fn(SimpleNamespace(complete_json=complete_json))

    created = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    config = SimpleNamespace(enrichment_provider="openai", enrichment_model=None)
    with (
        patch("core_api.clients.storage_client.get_storage_client", return_value=sc),
        patch("core_api.providers._retry.call_with_fallback", side_effect=fallback),
        patch("core_api.services.memory_service.create_memory", created),
    ):
        _reason, rule = await _generate_rule(
            tenant_id="t1",
            outcome="The deploy failed.",
            outcome_type="failure",
            related_ids=[str(uuid4())],
            config=config,
            agent_id="lead",
            fleet_id=None,
        )
        await _persist_rule("t1", "lead", None, rule, scope="agent")

    created.assert_awaited_once()
    assert created.await_args.kwargs.get("system_values") == {"source_trust": 1}


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def _row(agent_id: str, fleet_id: str | None, visibility: str) -> dict:
    return {
        "id": str(uuid4()),
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "visibility": visibility,
    }


def _finding(headline: str, cited: list[dict]) -> dict:
    return {
        "headline": headline,
        "what_happened": "Two teams describe the release train differently.",
        "recommended_action": "Confirm the cadence with the release owner.",
        "confidence": 0.8,
        "related_memory_ids": [row["id"] for row in cited],
    }


async def _persist(monkeypatch, *, scope: str, fleet_id: str | None, findings, rows):
    """Persist ``findings`` and return each one's (fleet, visibility, trust)."""
    import core_api.services.memory_service as ms_mod
    from core_api.services import insights_service

    calls: list = []

    async def bulk(data, *, bulk_attempt_id, **kw):
        calls.append((data, kw))
        results = [
            SimpleNamespace(index=i, id=uuid4(), error=None)
            for i in range(len(data.items))
        ]
        return SimpleNamespace(results=results)

    sc = AsyncMock()
    sc.insights_supersede_priors = AsyncMock(return_value={"prior_ids": []})
    sc.get_agent = AsyncMock(side_effect=_get_agent)
    monkeypatch.setattr(ms_mod, "create_memories_bulk", bulk)
    monkeypatch.setattr(insights_service, "get_storage_client", lambda: sc)
    monkeypatch.setattr(
        "core_api.clients.storage_client.get_storage_client", lambda: sc
    )
    ids = await insights_service._persist_findings(
        "t1",
        "lead",
        fleet_id,
        "discover",
        scope,
        findings,
        sources={row["id"]: row for row in rows},
    )
    assert all(ids), ids
    return {
        item.metadata["headline"]: (data.fleet_id, data.visibility, values)
        for data, kw in calls
        for item, values in zip(data.items, kw["system_values"], strict=True)
    }


def _rows() -> dict[str, dict]:
    return {
        "A low": _row("low", "A", "scope_team"),
        "A high": _row("high", "A", "scope_team"),
        "B high": _row("high", "B", "scope_team"),
        "org": _row("high", None, "scope_org"),
    }


@pytest.mark.parametrize(
    ("read", "home"),
    [
        (["A low", "A high", "org"], ("A", "scope_team", 1)),
        (["org"], (None, "scope_org", 2)),
        (["A high", "B high"], (None, "scope_agent", 2)),
    ],
    ids=["one fleet and org-wide", "org-wide only", "across fleets"],
)
async def test_a_scope_all_run_saves_in_the_narrowest_home_its_rows_allow(
    monkeypatch, read, home
):
    rows = _rows()
    shown = [rows[label] for label in read]
    homes = await _persist(
        monkeypatch,
        scope="all",
        fleet_id=None,
        rows=shown,
        findings=[_finding("finding", shown)],
    )

    fleet, visibility, trust = home
    assert homes == {"finding": (fleet, visibility, {"source_trust": trust})}


async def test_what_a_finding_cites_widens_neither_its_home_nor_its_trust(
    monkeypatch,
):
    """Stored text can steer which ids the model cites, and a finding may draw on
    any row the run read. So a finding citing only the trusted org-wide row of a
    run that also read fleet A's low-trust row and fleet B's row stays with the
    requester at the lowest trust."""
    rows = _rows()
    homes = await _persist(
        monkeypatch,
        scope="all",
        fleet_id=None,
        rows=[rows["A low"], rows["B high"], rows["org"]],
        findings=[
            _finding("cites org-wide only", [rows["org"]]),
            _finding("cites fleet B only", [rows["B high"]]),
        ],
    )

    assert homes == {
        "cites org-wide only": (None, "scope_agent", {"source_trust": 1}),
        "cites fleet B only": (None, "scope_agent", {"source_trust": 1}),
    }


async def test_a_fleet_scope_finding_keeps_its_fleet_and_records_trust(monkeypatch):
    rows = _rows()
    homes = await _persist(
        monkeypatch,
        scope="fleet",
        fleet_id="A",
        rows=[rows["A low"], rows["A high"]],
        findings=[
            _finding("cites the trusted row", [rows["A high"]]),
            _finding("cites none", []),
        ],
    )

    assert homes == {
        "cites the trusted row": ("A", "scope_team", {"source_trust": 1}),
        "cites none": ("A", "scope_team", {"source_trust": 1}),
    }


# ---------------------------------------------------------------------------
# The write path and the platform-only key
# ---------------------------------------------------------------------------


async def test_create_memory_sets_platform_values_after_sanitising_the_callers(
    monkeypatch,
):
    from core_api.schemas import MemoryCreate
    from core_api.services import memory_service

    seen: list = []

    async def pipeline(data, *, is_inferred=False):
        seen.append(data)
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(memory_service, "_run_write_pipeline", pipeline)
    monkeypatch.setattr(
        "core_api.services.value_ledger.schedule_origin", lambda *_a, **_kw: None
    )
    data = MemoryCreate(
        tenant_id="t1",
        agent_id="writer-1",
        content="The release train runs weekly.",
        metadata={"source_trust": 3, "note": "kept"},
    )
    await memory_service.create_memory(data, system_values={"source_trust": 1})

    (stored,) = seen
    assert stored.metadata["_system"]["source_trust"] == 1
    assert stored.metadata["source_trust"] == 1
    assert stored.metadata["note"] == "kept"


def test_callers_cannot_write_source_trust():
    from core_api.services.system_metadata import sanitize_caller_metadata

    forged = {"source_trust": 3, "_system": {"source_trust": 3}, "note": "kept"}
    assert sanitize_caller_metadata(forged) == {"note": "kept"}
