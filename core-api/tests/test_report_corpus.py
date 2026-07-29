"""Unit tests for the shared report-corpus helpers (CAURA-222)."""

from __future__ import annotations

from core_api.services.report_corpus import is_cohesive, passes_noise_filter, resolve_parent


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
