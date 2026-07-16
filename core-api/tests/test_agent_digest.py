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

    async def prune_agent_activity_digests(self, tenant_id: str, older_than: str) -> int:
        self.pruned: tuple[str, str] = (tenant_id, older_than)
        return 3


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
    """Return a helper that wires a FakeStorage + a deterministic LLM.

    The default LLM mock simulates the REAL provider succeeding (returns a dict
    directly, without touching the fake tier) so ``used_fallback`` stays False
    and status is ``ok``/``truncated``. The LLM-unavailable path is exercised
    separately in ``test_llm_unavailable_is_skipped_no_row``."""

    async def _real_cwf(provider, call_fn, fake_fn, **kw):
        return {"narrative": "real summary", "shipped": ["shipped a thing"]}

    monkeypatch.setattr(agent_digest, "call_with_fallback", _real_cwf)

    def _install(storage: FakeStorage) -> FakeStorage:
        monkeypatch.setattr(agent_digest, "get_storage_client", lambda: storage)
        return storage

    return _install


async def test_generates_only_for_agents_above_threshold(wire):
    # period="week" so the configured min_activity_threshold=3 applies (daily is
    # floored to 1 — see test_daily_threshold_is_one).
    storage = wire(
        FakeStorage(
            [{"agent_id": "a", "fleet_id": None}, {"agent_id": "b", "fleet_id": "f1"}],
            {"a": [_mem(agent="a")] * 4, "b": [_mem(agent="b")] * 2},  # b below min_activity=3
        )
    )
    summary = await agent_digest.generate_for_org("org1", "week", CONFIG, now=NOW)
    assert summary["generated"] == 1
    assert [r["agent_id"] for r in storage.upserts] == ["a"]
    assert storage.upserts[0]["status"] == "ok"
    assert storage.upserts[0]["source_count"] == 4


async def test_daily_threshold_is_one(wire):
    """Daily windows floor the activity threshold to 1 regardless of config, so a
    barely-active agent still gets a daily digest (weekly would exclude it)."""
    seed = {"a": [_mem(agent="a")] * 2, "b": [_mem(agent="b")] * 1}
    agents = [{"agent_id": "a", "fleet_id": None}, {"agent_id": "b", "fleet_id": None}]

    day = wire(FakeStorage(agents, seed))
    s_day = await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    assert s_day["generated"] == 2  # both included at floor 1
    assert sorted(r["agent_id"] for r in day.upserts) == ["a", "b"]

    week = wire(FakeStorage(agents, seed))
    s_week = await agent_digest.generate_for_org("org1", "week", CONFIG, now=NOW)
    assert s_week["generated"] == 0  # both below the week floor of 3


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
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 5}))
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


async def test_llm_unavailable_is_skipped_no_row(monkeypatch, wire):
    """When call_with_fallback drops to the template tier (LLM unavailable), the
    agent is skipped: NO row is written (no generic placeholder), and it counts
    as skipped, not generated/errored."""
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 4}))

    async def _fallback_cwf(provider, call_fn, fake_fn, **kw):
        return fake_fn()  # real + fallback providers failed → template tier

    monkeypatch.setattr(agent_digest, "call_with_fallback", _fallback_cwf)
    summary = await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    assert storage.upserts == []  # no placeholder row persisted
    assert summary["skipped"] == 1
    assert summary["generated"] == 0
    assert summary["errored"] == 0


async def test_daily_window_is_previous_utc_day(wire):
    """A mid-day run normalizes to the full previous UTC calendar day."""
    now = datetime(2026, 7, 6, 15, 30, tzinfo=UTC)
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 3}))
    await agent_digest.generate_for_org("org1", "day", CONFIG, now=now)
    row = storage.upserts[0]
    assert row["window_end"] == "2026-07-06T00:00:00+00:00"  # today 00:00
    assert row["window_start"] == "2026-07-05T00:00:00+00:00"  # yesterday 00:00


async def test_weekly_window_aligns_to_monday(wire):
    """A mid-week run normalizes to the previous full Mon-Mon week."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)  # a Wednesday
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 3}))
    await agent_digest.generate_for_org("org1", "week", CONFIG, now=now)
    row = storage.upserts[0]
    assert row["window_end"] == "2026-07-06T00:00:00+00:00"  # Monday of that week
    assert row["window_start"] == "2026-06-29T00:00:00+00:00"  # previous Monday


async def test_empty_narrative_is_skipped(monkeypatch, wire):
    """A real response missing the narrative is skipped (no row) rather than
    persisted with a synthesized placeholder."""
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 3}))

    async def _empty_cwf(provider, call_fn, fake_fn, **kw):
        return {"shipped": ["x", "y"]}  # sections present, no narrative key

    monkeypatch.setattr(agent_digest, "call_with_fallback", _empty_cwf)
    summary = await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    assert storage.upserts == []
    assert summary["skipped"] == 1
    assert summary["generated"] == 0
    assert summary["errored"] == 0


async def test_upsert_failure_counts_as_errored(wire):
    """An upsert failure re-raises and is counted as errored, not generated."""

    class FailingStorage(FakeStorage):
        async def upsert_agent_activity_digest(self, row: dict) -> dict:
            raise RuntimeError("db down")

    wire(FailingStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 3}))
    summary = await agent_digest.generate_for_org("org1", "day", CONFIG, now=NOW)
    assert summary["errored"] == 1
    assert summary["generated"] == 0


async def test_retention_prune_runs_with_cutoff(wire):
    storage = wire(FakeStorage([{"agent_id": "a", "fleet_id": None}], {"a": [_mem(agent="a")] * 3}))
    summary = await agent_digest.generate_for_org("org1", "day", {**CONFIG, "retention_days": 30}, now=NOW)
    assert summary["pruned"] == 3
    assert storage.pruned == ("org1", "2026-06-06T00:00:00+00:00")  # NOW - 30d


async def test_run_agent_digest_rejects_bad_period(wire):
    with pytest.raises(ValueError):
        await agent_digest.run_agent_digest("month")
