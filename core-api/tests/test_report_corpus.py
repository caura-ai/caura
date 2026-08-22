"""Unit tests for the shared report-corpus helpers (CAURA-222)."""

from __future__ import annotations

from core_api.services.report_corpus import (
    is_cohesive,
    is_reserved_agent,
    passes_noise_filter,
    resolve_parent,
)


def test_resolve_parent_explicit_subagent_convention():
    known = frozenset({"quantclaw"})
    assert resolve_parent("agent:quantclaw:subagent:4ce6c6c9", known) == "quantclaw"
    # parent need not be a registered agent — still resolves to the derived name
    assert resolve_parent("agent:devclaw:subagent:x", frozenset()) == "devclaw"


def test_resolve_parent_prefix_convention_longest_and_transitive():
    known = frozenset({"cmoclaw", "cmoclaw-affiliate", "other"})
    # longest known prefix, then resolved transitively to the root
    assert resolve_parent("cmoclaw-affiliate-ai-fix", known) == "cmoclaw"
    assert resolve_parent("cmoclaw-a11y-audit", known) == "cmoclaw"


def test_resolve_parent_boundary_guard():
    # 'cmo' must not swallow 'cmoclaw' — the boundary is a '-'
    assert resolve_parent("cmoclaw", frozenset({"cmo"})) == "cmoclaw"


def test_resolve_parent_top_level_is_itself():
    known = frozenset({"standalone", "other"})
    assert resolve_parent("standalone", known) == "standalone"


def _m(mtype="decision", title="did a thing", agent="a"):
    return {"memory_type": mtype, "title": title, "agent_id": agent}


def test_filter_split_durable_vs_events_vs_noise():
    durable = _m()
    episode = _m(mtype="episode")
    heartbeat = _m(title="heartbeat check")
    firehose = _m(agent="main")

    # durable: passes noise AND not episode
    assert is_cohesive(durable) and passes_noise_filter(durable)
    # episode: real signal (passes noise) but NOT cohesive -> the events fallback
    assert passes_noise_filter(episode) and not is_cohesive(episode)
    # heartbeat title: dropped by both
    assert not passes_noise_filter(heartbeat) and not is_cohesive(heartbeat)
    # firehose agent: dropped by both
    assert not passes_noise_filter(firehose) and not is_cohesive(firehose)


def test_is_reserved_agent_predicate():
    assert is_reserved_agent("main")
    assert is_reserved_agent("__health_check__")
    assert is_reserved_agent("__anything__")  # reserved-system __name__ convention
    # a real, unconfigured-identity agent (default main-<hex>) is NOT reserved
    assert not is_reserved_agent("main-e5366d79a926")
    assert not is_reserved_agent("quantclaw")
    assert not is_reserved_agent("")
    assert not is_reserved_agent(None)


def test_health_check_probe_dropped_as_noise():
    # __health_check__ writes durable-typed 'fact' rows titled 'memclaw-smoke-*';
    # both the reserved-agent gate and the title regex must drop them so the probe
    # never tops the leaderboard.
    probe = _m(mtype="fact", title="memclaw-smoke-1786983933574 identifier", agent="__health_check__")
    assert not passes_noise_filter(probe) and not is_cohesive(probe)
    # the memclaw-smoke title alone drops it even under a non-reserved id
    smoke_titled = _m(mtype="fact", title="memclaw-smoke-123 identifier", agent="someagent")
    assert not passes_noise_filter(smoke_titled)


def test_reserved_id_never_becomes_a_rollup_parent():
    # bare 'main' is still a known id (historical firehose rows), but an
    # unconfigured 'main-<hex>' agent must resolve to ITSELF — not be mislabeled
    # under the retired 'main' firehose.
    known = frozenset({"main", "main-e5366d79a926", "quantclaw"})
    assert resolve_parent("main-e5366d79a926", known) == "main-e5366d79a926"
    # the structured agent:<parent>:subagent: form can't smuggle a reserved parent
    assert resolve_parent("agent:main:subagent:abc", known) == "agent:main:subagent:abc"
    # a reserved '__probe__' prefix is likewise never a parent
    assert resolve_parent("__probe__-task", frozenset({"__probe__"})) == "__probe__-task"


def test_legit_rollup_unaffected_by_reserved_skip():
    # skipping reserved prefixes must not disturb normal family resolution
    known = frozenset({"main", "cmoclaw", "cmoclaw-affiliate"})
    assert resolve_parent("cmoclaw-affiliate-ai-fix", known) == "cmoclaw"
