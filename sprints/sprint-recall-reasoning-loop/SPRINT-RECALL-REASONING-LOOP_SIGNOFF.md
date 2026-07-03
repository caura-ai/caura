# Sprint Signoff: Recall Reasoning Loop

**Sprint Goal:** Give `memclaw_recall` an opt-in, agentic multi-step reasoning loop over the
knowledge graph — inspired by MRAgent's typed multi-tool query pattern — without touching the
default single-pass hybrid search or its documented 23ms p50 latency benchmark.

**Sprint Duration:** 2026-07-03T07:34+07:00 → 2026-07-03T13:04+07:00 (~5.5 hours wall-clock)

**Branch:** `sprint/recall-reasoning-loop-plan`

## Execution Summary

| Metric | Value |
|---|---|
| Total WorkItems Planned | 6 (RL-01 … RL-05, RL-Z01) |
| Completed | 6 |
| Failed / Cancelled | 0 |
| Success Rate | 6 / 6 (100%) |
| Total Attempts | 6 (every item completed on its first attempt) |
| Retry Rate | 0% |
| Wall-Clock Duration | ~5.5 hours |
| Critical Path | RL-01 → RL-02 → RL-03 → RL-04/RL-05 (parallel-eligible) → RL-Z01 |

## WorkItem Status

| ID | Title | Status | Attempts | Commit | Notes |
|---|---|---|---|---|---|
| RL-01 | Typed graph-query tool wrappers (`graph_query_tools.py`) | completed | 1 | `e56ec76` | Wraps existing, already-tested storage primitives — no new storage-layer code |
| RL-02 | Bounded reasoning-loop orchestrator (`graph_reasoning_loop.py`) | completed | 1 | `e56ec76` | `GRAPH_REASONING_MAX_ITERATIONS = 3`, never raises to caller |
| RL-03 | Wire `reasoning_mode` opt-in through `mcp_server` → `memory_service` → pipeline | completed | 1 | `42b2c46` (fixed from `da58ae2`/`13dd08d`) | Corrected mid-sprint: dropped a dead `recall_service.recall()` hop found during verification |
| RL-04 | Latency-regression guard test | completed | 1 | `ae5d830` | `reasoning_mode=False` median 7.59ms vs 7.89ms no-param baseline (0.962x, ceiling 1.10x) |
| RL-05 | Accuracy/behavior tests (temporal + relational tool selection, termination, error handling) | completed | 1 | `06c1fb7` | 4/4 tests pass against real Postgres in 0.48s |
| RL-Z01 | This signoff document | completed | 1 | (this commit) | |

## Acceptance Criteria — Verified

**RL-04** (`tests/pipeline/test_recall_reasoning_loop_latency.py`):
- `reasoning_mode=False` p50 (7.59ms) stayed within the 1.10x ceiling of the no-param baseline
  (7.89ms) — ratio 0.962x, i.e. *faster*, not slower, within noise.
- `reasoning_mode=True` measured and logged only (7.23ms median) — not gated, per plan.
- `tests/pipeline/test_pipeline_latency.py` (the pre-existing benchmark) still passes unmodified.

**RL-05** (`tests/services/test_graph_reasoning_loop.py`):
- `test_temporal_query_picks_time_range` — temporal-phrased query drives the loop to
  `query_by_time_range`.
- `test_relational_query_picks_graph_context` — relational-phrased query drives the loop to
  `query_by_graph_context`.
- `test_loop_terminates_at_max_iterations` — loop stops at `GRAPH_REASONING_MAX_ITERATIONS = 3`
  even when the scripted provider never emits `"done"`.
- `test_provider_error_returns_empty_no_raise` — a dispatch-time exception degrades to an empty
  boosted set (`boost_factor = 0.0`) instead of propagating.
- All 4 tests pass: `4 passed in 0.48s`.

## Artifacts Produced

- `core-api/src/core_api/services/graph_query_tools.py` — four typed tool wrappers
- `core-api/src/core_api/services/graph_reasoning_loop.py` — bounded orchestration loop
- `reasoning_mode: bool = False` param threaded through `mcp_server.memclaw_recall` →
  `memory_service.search_memories()` → pipeline, additive only
- `tests/pipeline/test_recall_reasoning_loop_latency.py` — latency guard (RL-04)
- `tests/services/test_graph_reasoning_loop.py` — behavior tests (RL-05)
- This signoff document (RL-Z01)

## Failure Analysis

No WorkItem failed or required a retry. One mid-sprint self-correction occurred during RL-03: the
initial wiring routed through a `recall_service.recall()` hop that turned out to be dead code not
actually on the call path from `mcp_server.memclaw_recall`; this was caught during verification
(commits `da58ae2`, `13dd08d`) and fixed before RL-03 was marked completed (`42b2c46`). Not a
failure in the retry-taxonomy sense — no test ever failed — but worth recording as a planning
lesson.

## Lessons Learned

### What Went Well

1. **Grounded reuse paid off.** All three storage-facing typed tools (`query_by_keyword`,
   `query_by_graph_context`, `query_by_time_range`) wrapped already-tested primitives, so RL-01
   needed no new storage-layer code and RL-05 could stub them out entirely via monkeypatch
   without a correctness risk — RL-01's own tests already cover that layer.
   *(`query_by_entity_links` also shipped, extending the pattern beyond the three originally
   scoped.)*
2. **The "never raises to the caller" invariant made both test files simple.** RL-02's design
   (catch-and-degrade at both the per-tool-dispatch level and the outer loop level) meant RL-05
   could assert exact empty-result shapes on failure instead of needing `pytest.raises`.
   gymnastics.
3. **Ratio-based latency assertions avoided CI flakiness.** Following `test_pipeline_latency.py`'s
   existing pattern (percentage ceiling vs. absolute ms) meant RL-04 needed no tuning to pass
   reliably.

### What To Improve

1. **RL-03's dead-hop discovery should have been caught at plan-verification time**, not during
   implementation. The plan cited the real call chain (`mcp_server.memclaw_recall` →
   `memory_service.search_memories()`) but an earlier read of the codebase had assumed an
   intermediate `recall_service.recall()` layer existed on the hot path. Future plans should grep
   the actual call site (not just the function definition) before citing a wiring chain.
   **Action:** add "trace one real call site, not just the function signature" to the
   plan-verification checklist for wiring tasks.
2. **Local test invocation has two silent traps** discovered during RL-05: the repo requires
   `.venv` activation (system Python lacks `pgvector`), and `tests/conftest.py`'s
   `TEST_DATABASE_URL` default (port 5432) doesn't match the actual `memclaw-test-db` Docker
   container's mapped port (5433). Every test in the suite is forced through real-Postgres setup
   via an `autouse=True` fixture regardless of whether the test function requests a `db` param.
   **Action:** document the working invocation recipe (below) so future sprints don't rediscover
   it.
   ```bash
   source .venv/bin/activate && \
   TEST_DATABASE_URL="postgresql+asyncpg://memclaw:changeme@127.0.0.1:5433/memclaw" \
   python -m pytest tests/ -q
   ```

## Backlog (deferred by explicit scope decision — see plan)

1. **Auto-trigger heuristic for `reasoning_mode`.** v1 requires the caller to pass
   `reasoning_mode=True` explicitly. A classifier that detects relational/temporal-looking
   queries and silently switches retrieval strategy was deliberately deferred — it's new, untested
   surface area and risks the exact latency regression this sprint was scoped to avoid if it ran
   on every query.
2. **`query_by_tag` typed tool.** MRAgent's fourth analog-less tool. No tag-search primitive
   exists anywhere in `postgres_service.py`; building one is an unscoped storage-layer change.
3. **Per-org configurable `max_iterations`/timeouts.** Currently hardcoded constants
   (`GRAPH_REASONING_MAX_ITERATIONS = 3`) in `graph_reasoning_loop.py`, unlike `graph_max_hops`
   which is already a per-org config field. Small follow-up if usage data shows 3 iterations is
   too tight or too loose in practice.

## Recommendations for Next Sprint

1. If usage telemetry shows `reasoning_mode=True` gets meaningful uptake, revisit the auto-trigger
   heuristic backlog item — with its own latency-guard test modeled directly on RL-04's.
2. Consider promoting the `.venv` + `TEST_DATABASE_URL` invocation recipe into a `Makefile` target
   or a short note in the repo's test-running docs so it isn't rediscovered per-sprint.

---

**Signed off:** Claude (sprint-run execution)

**Date:** 2026-07-03

**Next Sprint:** Ready to plan — no blocking follow-ups; all backlog items are opt-in enhancements,
not defects.
