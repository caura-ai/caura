"""RE-04: behavior + regression tests for the RE-03 ``verbosity`` projection.

Asserts through the public MCP tool surface (``core_api.mcp_server`` handlers,
stubbed via ``tests/_mcp_test_helpers``), complementing the per-tool tests in
``test_mcp_recall.py`` / ``test_mcp_list.py`` / ``test_mcp_session.py`` with the
cross-cutting guarantees the sprint plan pins:

* compact shape is EXACT — the projected key set is precisely the compact field
  set and carries none of the full-``MemoryOut`` bookkeeping (AC2);
* the default (no-param) response key set equals the full ``MemoryOut`` field
  set, proving the param's mere existence didn't alter today's shape (AC3);
* ``memclaw_list`` / ``memclaw_session_start`` project identically, minus
  ``similarity`` which only the recall path carries (AC4);
* compact and full cache entries don't cross-contaminate (AC5) — this test
  fails on the pre-RE-03 single-keyed cache by construction;
* the unused-param default path carries no measurable latency overhead (AC6);
* the compact payload is materially smaller than the full one (AC7, logged).

Unit-scale note (AC6): AC1 scopes this file to the MCP tool surface (stubbed),
so the latency guard is modelled on the *structure* of
``tests/pipeline/test_recall_reasoning_loop_latency.py`` (warmup, same-run
baseline, ratio, never absolute ms) but runs at handler scale. At that scale
medians are microseconds and noisier than that integration test's ms medians,
so the ceiling is 1.25x rather than 1.10x — still a real guard (the default and
explicit-``full`` calls provably take the same ``else`` branch, so the true
ratio is ~1.0), just with headroom against GC/scheduler jitter.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core_api import mcp_server
from core_api.schemas import MemoryOut
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import uid

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ── Fixtures / helpers ───────────────────────────────────────────────

# The exact compact projections (mirrors mcp_server._COMPACT_FIELDS).
_RECALL_COMPACT_KEYS = {
    "id",
    "title",
    "content",
    "memory_type",
    "status",
    "weight",
    "created_at",
    "similarity",
}
_NON_SIMILARITY_COMPACT_KEYS = _RECALL_COMPACT_KEYS - {"similarity"}

# Full-MemoryOut fields that compact must DROP (a representative, load-bearing
# subset of the bookkeeping the plan calls out explicitly).
_MUST_BE_DROPPED = {
    "tenant_id",
    "entity_links",
    "run_id",
    "usage",
    "ts_valid_start",
    "ts_valid_end",
    "subject_entity_id",
    "predicate",
    "object_value",
    "recall_count",
    "last_recalled_at",
    "supersedes_id",
    "superseded_by",
    "visibility",
    "source_type",
}


def _make_memory_out(*, similarity: float | None = 0.9, **overrides) -> MemoryOut:
    """A fully-populated ``MemoryOut`` so the full dump carries every field and
    the compact projection has real bookkeeping to strip."""
    base: dict = dict(
        id=uuid4(),
        tenant_id="test-tenant",
        fleet_id="test-fleet",
        agent_id="test-agent",
        memory_type="decision",
        title=f"decision-{uid()}",
        content=f"tenant isolation scoping note {uid()}",
        weight=0.5,
        source_uri=None,
        run_id=f"run-{uid()}",
        metadata={"k": "v"},
        created_at=datetime.now(timezone.utc),
        expires_at=None,
        similarity=similarity,
        status="active",
        recall_count=3,
    )
    base.update(overrides)
    return MemoryOut(**base)


def _wire_recall(monkeypatch, *, results, cached=None):
    """Wire recall's storage-routed deps + cache, returning the search mock."""

    class _Cfg:
        recall_boost = False
        graph_expand = False

    async def _cfg(_tenant):
        return _Cfg()

    monkeypatch.setattr(mcp_server, "resolve_config", _cfg)
    monkeypatch.setattr(mcp_server, "cache_get", AsyncMock(return_value=cached))
    monkeypatch.setattr(mcp_server, "cache_set", AsyncMock(return_value=True))
    stub_storage_client(monkeypatch, get_agent=None)
    search = AsyncMock(return_value=results)
    monkeypatch.setattr(mcp_server, "search_memories", search)
    return search


# ── AC2 / AC3: recall compact exactness + full default unchanged ─────


async def test_compact_shape_exact(mcp_env, monkeypatch):
    """A compact recall result is EXACTLY the compact field set and drops all
    full-MemoryOut bookkeeping."""
    _wire_recall(monkeypatch, results=[_make_memory_out()])

    out = await mcp_server.memclaw_recall(query="tenant isolation", verbosity="compact")
    result = parse_envelope(out)["results"][0]

    assert set(result.keys()) == _RECALL_COMPACT_KEYS
    assert _MUST_BE_DROPPED & set(result.keys()) == set()


async def test_full_default_unchanged(mcp_env, monkeypatch):
    """No verbosity param → per-memory key set equals the full MemoryOut field
    set (the param's existence did not alter today's default response)."""
    _wire_recall(monkeypatch, results=[_make_memory_out()])

    out = await mcp_server.memclaw_recall(query="tenant isolation")
    result = parse_envelope(out)["results"][0]

    assert set(result.keys()) == set(MemoryOut.model_fields.keys())


# ── AC4: list + session_start compact ────────────────────────────────


async def test_list_compact_shape_exact(mcp_env, monkeypatch):
    """memclaw_list compact result carries the compact set WITHOUT similarity."""
    rows = [{"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(
        mcp_server, "_memory_to_out", lambda m: _make_memory_out(similarity=None)
    )

    out = await mcp_server.memclaw_list(verbosity="compact")
    result = parse_envelope(out)["results"][0]

    assert set(result.keys()) == _NON_SIMILARITY_COMPACT_KEYS
    assert _MUST_BE_DROPPED & set(result.keys()) == set()


async def test_session_start_compact_shape_exact(mcp_env, monkeypatch):
    """memclaw_session_start compact memory carries the compact set (no similarity)."""
    row = {"id": str(uuid4()), "content": "c"}
    stub_storage_client(
        monkeypatch,
        list_memories_by_filters=[row],
        list_keystones=([], False),
        list_procedures=[],
    )
    monkeypatch.setattr(
        mcp_server, "_memory_to_out", lambda m: _make_memory_out(similarity=None)
    )

    out = await mcp_server.memclaw_session_start(verbosity="compact")
    memory = parse_envelope(out)["memories"][0]

    assert set(memory.keys()) == _NON_SIMILARITY_COMPACT_KEYS


# ── AC5: cache no cross-contamination ────────────────────────────────


async def test_cache_no_cross_contamination(mcp_env, monkeypatch):
    """The same query issued compact-then-full (and full-then-compact) returns
    the correct shape each time. On the pre-RE-03 single-keyed cache the second
    call would be served the first call's cached shape — this fails there.

    A real cache is emulated with a dict keyed by whatever key the handler
    computes, so the guarantee is exercised through the actual cache round-trip,
    not just a key-inequality assertion.
    """
    store: dict[str, str] = {}

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ttl=None):  # noqa: ARG001
        store[key] = value
        return True

    class _Cfg:
        recall_boost = False
        graph_expand = False

    async def _cfg(_tenant):
        return _Cfg()

    monkeypatch.setattr(mcp_server, "resolve_config", _cfg)
    monkeypatch.setattr(mcp_server, "cache_get", _get)
    monkeypatch.setattr(mcp_server, "cache_set", _set)
    stub_storage_client(monkeypatch, get_agent=None)
    monkeypatch.setattr(
        mcp_server, "search_memories", AsyncMock(return_value=[_make_memory_out()])
    )

    q = f"tenant isolation {uid()}"

    # compact first (populates a compact-keyed cache entry), then full.
    compact1 = parse_envelope(
        await mcp_server.memclaw_recall(query=q, verbosity="compact")
    )
    full1 = parse_envelope(await mcp_server.memclaw_recall(query=q, verbosity="full"))
    assert set(compact1["results"][0].keys()) == _RECALL_COMPACT_KEYS
    assert set(full1["results"][0].keys()) == set(MemoryOut.model_fields.keys())

    # reverse order, fresh query: full first, then compact.
    q2 = f"tenant isolation {uid()}"
    full2 = parse_envelope(await mcp_server.memclaw_recall(query=q2, verbosity="full"))
    compact2 = parse_envelope(
        await mcp_server.memclaw_recall(query=q2, verbosity="compact")
    )
    assert set(full2["results"][0].keys()) == set(MemoryOut.model_fields.keys())
    assert set(compact2["results"][0].keys()) == _RECALL_COMPACT_KEYS


# ── AC6: default-path latency guard (ratio, never absolute ms) ────────

_ITERATIONS = 50
_DEFAULT_PATH_OVERHEAD_RATIO_MAX = 1.25


async def test_default_path_no_latency_overhead(mcp_env, monkeypatch):
    """No-verbosity recall p50 stays within the ratio ceiling of the explicit
    verbosity='full' call — both take the same ``else _dumped`` branch, so the
    new parameter adds no systematic overhead to the default path."""
    _wire_recall(monkeypatch, results=[_make_memory_out() for _ in range(5)])

    # Warmup (prime imports/attribute lookups).
    await mcp_server.memclaw_recall(query="warmup")

    baseline = []
    for _ in range(_ITERATIONS):
        t0 = time.perf_counter()
        await mcp_server.memclaw_recall(query="tenant isolation")  # no param
        baseline.append((time.perf_counter() - t0) * 1000)

    explicit_full = []
    for _ in range(_ITERATIONS):
        t0 = time.perf_counter()
        await mcp_server.memclaw_recall(query="tenant isolation", verbosity="full")
        explicit_full.append((time.perf_counter() - t0) * 1000)

    base_median = statistics.median(baseline)
    full_median = statistics.median(explicit_full)
    ratio = full_median / base_median if base_median > 0 else float("inf")

    print(f"\n{'=' * 60}")
    print("VERBOSITY DEFAULT-PATH OVERHEAD CHECK")
    print(f"no-param baseline    — median: {base_median:.4f}ms")
    print(f"verbosity='full'     — median: {full_median:.4f}ms")
    print(f"ratio — {ratio:.3f}x (ceiling {_DEFAULT_PATH_OVERHEAD_RATIO_MAX}x)")
    print(f"{'=' * 60}")

    assert full_median <= base_median * _DEFAULT_PATH_OVERHEAD_RATIO_MAX, (
        f"verbosity='full' median {full_median:.4f}ms is {ratio:.2f}x the no-param "
        f"baseline {base_median:.4f}ms, exceeds {_DEFAULT_PATH_OVERHEAD_RATIO_MAX}x."
    )


# ── AC7: compact payload size evidence (logged, soft-gated) ───────────


async def test_compact_payload_smaller_than_full(mcp_env, monkeypatch):
    """A 5-result compact recall serializes to <= 50% of the full payload's
    character count. Informational evidence for the signoff (printed)."""
    results = [_make_memory_out() for _ in range(5)]

    _wire_recall(monkeypatch, results=results)
    full_out = await mcp_server.memclaw_recall(
        query="tenant isolation", verbosity="full"
    )
    full_chars = len(json.dumps(parse_envelope(full_out)))

    _wire_recall(monkeypatch, results=results)
    compact_out = await mcp_server.memclaw_recall(
        query="tenant isolation", verbosity="compact"
    )
    compact_chars = len(json.dumps(parse_envelope(compact_out)))

    ratio = compact_chars / full_chars if full_chars else float("inf")
    print(f"\n{'=' * 60}")
    print("COMPACT vs FULL PAYLOAD SIZE (5-result recall)")
    print(f"full:    {full_chars} chars")
    print(f"compact: {compact_chars} chars  ({ratio:.1%} of full)")
    print(f"{'=' * 60}")

    assert compact_chars <= full_chars * 0.5, (
        f"compact payload {compact_chars} chars is {ratio:.1%} of full {full_chars} — "
        "expected <= 50%"
    )
