"""Unit tests for the agent-digest generator (Phase 2b).

Storage + LLM are mocked, so these run without a DB or API key and assert the
generation LOGIC: cohesive filtering, activity threshold, top-N + cost budget,
truncation, fleet passthrough, and the enumerate→generate fanout.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core_api.services import agent_digest

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
CONFIG = {"top_n": 25, "max_memories_per_agent": 60, "min_activity_threshold": 3, "model": "gpt-5.4-mini"}


class FakeStorage:
    def __init__(self, agents: list[dict], mems_by_agent: dict[str, list[dict]]):
        self._agents = agents
        self._mems = mems_by_agent
        self.upserts: list[dict] = []

    async def list_agents(self, org_id: str, fleet_id: str | None = None) -> list[dict]:
        return self._agents

    async def list_memories_by_filters(self, query: dict) -> list[dict]:
        return self._mems.get(query["written_by"], [])

    async def upsert_agent_activity_digest(self, row: dict) -> dict:
        self.upserts.append(row)
        return row


def _mem(*, mtype: str = "decision", title: str = "did a thing", recall: int = 1, agent: str = "a") -> dict:
    return {
        "memory_type": mtype,
        "title": title,
        "recall_count": recall,
        "created_at": "2026-07-05T10:00:00+00:00",
        "agent_id": agent,
    }


@pytest.fixture
def wire(monkeypatch):
    """Return a helper that wires a FakeStorage + a deterministic LLM (fake tier)."""

    async def _fake_cwf(provider, call_fn, fake_fn, **kw):
        return fake_fn()

    monkeypatch.setattr(agent_digest, "call_with_fallback", _fake_cwf)

    def _install(storage: FakeStorage) -> FakeStorage:
        monkeypatch.setattr(agent_digest, "get_storage_client", lambda: storage)
        return storage

    return _install


async def test_generates_only_for_agents_above_threshold(wire):
    storage = wire(
        FakeStorage(
            [{"agent_id": "a", "fleet_id": None}, {"agent_id": "b", "fleet_id": "f1"}],
            {"a": [_mem(agent="a")] * 4, "b": [_mem(agent="b")] * 2},  # b below min_activity=3
        )
    )
    summary = await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    assert summary["generated"] == 1
    assert [r["agent_id"] for r in storage.upserts] == ["a"]
    assert storage.upserts[0]["status"] == "ok"
    assert storage.upserts[0]["source_count"] == 4


async def test_cohesive_filter_drops_noise_and_episodes(wire):
    storage = wire(
        FakeStorage(
            [{"agent_id": "a", "fleet_id": None}],
            {"a": [_mem(title="heartbeat check")] * 5 + [_mem(mtype="episode")] * 5 + [_mem()] * 3},
        )
    )
    await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    # Only the 3 real decision rows survive the cohesive filter.
    assert storage.upserts[0]["source_count"] == 3


async def test_top_n_limits_llm_calls_to_busiest(wire):
    storage = wire(
        FakeStorage(
            [{"agent_id": "busy", "fleet_id": None}, {"agent_id": "quieter", "fleet_id": None}],
            {"busy": [_mem(agent="busy")] * 10, "quieter": [_mem(agent="quieter")] * 4},
        )
    )
    summary = await agent_digest.generate_for_org("org1", "day", {**CONFIG, "top_n": 1}, now=NOW)
    assert summary["generated"] == 1
    assert storage.upserts[0]["agent_id"] == "busy"  # ranked by volume


async def test_cost_cap_trims_agents(wire):
    storage = wire(
        FakeStorage(
            [{"agent_id": f"a{i}", "fleet_id": None} for i in range(5)],
            {f"a{i}": [_mem(agent=f"a{i}")] * 4 for i in range(5)},
        )
    )
    # budget = max(1, 0.01 / 0.005) = 2 calls
    summary = await agent_digest.generate_for_org(
        "org1", "day", {**CONFIG, "max_cost_per_run_usd": 0.01}, now=NOW
    )
    assert summary["generated"] == 2


async def test_truncation_status_and_capped_source_count(wire):
    storage = wire(
        FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 5})
    )
    await agent_digest.generate_for_org("org1", "day", {**CONFIG, "max_memories_per_agent": 2}, now=NOW)
    assert storage.upserts[0]["status"] == "truncated"
    assert storage.upserts[0]["source_count"] == 2


async def test_fleet_id_passthrough_and_window(wire):
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": "etoro0"}], {"a": [_mem(agent="a")] * 3}))
    await agent_digest.generate_for_org("org1", "week", CONFIG, now=NOW)
    row = storage.upserts[0]
    assert row["fleet_id"] == "etoro0"
    assert row["window_end"] == NOW.isoformat()
    assert row["window_start"] == "2026-06-29T00:00:00+00:00"  # 7 days back
    assert row["period"] == "week"


async def test_run_agent_digest_enumerates_opted_in_orgs(monkeypatch, wire):
    import core_api.services.tenants as tenants_mod

    async def _list_orgs() -> list[str]:
        return ["org1", "org2"]

    async def _settings(org_id: str) -> dict:
        return {"agent_digest": {"enabled": True, **CONFIG}}

    monkeypatch.setattr(tenants_mod, "list_tenants_with_agent_digest_enabled", _list_orgs)
    monkeypatch.setattr(agent_digest, "get_settings_for_display", _settings)
    wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 4}))

    summary = await agent_digest.run_agent_digest("day")
    assert summary["orgs"] == 2
    assert summary["completed"] == 2
    assert summary["digests"] == 2


async def test_run_agent_digest_rejects_bad_period(wire):
    with pytest.raises(ValueError):
        await agent_digest.run_agent_digest("month")
