"""Session-trace builder (Skill Factory SF-102).

Produces one ``session_traces`` row per ``(tenant_id, run_id,
agent_id)`` group of memories within a scan window, labeled with an
inferred ``outcome_label`` derived from the 6 signal extractors in
:mod:`core_api.services.outcome_inference`.

This module is the bridge between the raw memory stream and Forge:
the cluster + fingerprint step (SF-103) reads from ``session_traces``,
not from ``memories`` directly. Centralising trace identity here
means the cluster step doesn't have to re-derive what counts as a
trace.

Algorithm (one pass per Forge tick):

  1. Run all 6 extractors concurrently over the window, collecting
     :class:`SignalEvidence` from each.
  2. Partition evidence by ``(run_id, agent_id)`` using
     ``evidence.details["run_id"]`` and ``...["agent_id"]``. Each
     extractor stamps these on every emitted evidence.
  3. Query ``memories`` in the window to enumerate every
     ``(run_id, agent_id)`` group — including groups with no signal
     evidence at all. Their traces label ``outcome_label='unknown'``
     and DO still get persisted (Forge filters them out later, but
     downstream consumers may want them for analytics).
  4. Fold per-trace evidence into an outcome label via
     :func:`fold_outcome_label`.
  5. Upsert rows into ``session_traces`` keyed by the
     ``(tenant_id, run_id, agent_id)`` unique constraint
     (migration 021).

Idempotency: re-running over the same window produces the same
rows. The upsert collapses repeated runs into one row per trace.
Multiple extractors firing on the same evidence (e.g. supersession
+ contradiction both pointing at the same memory) accumulate in
``signals_summary`` without inflating the label — the fold uses the
WEIGHTED SUM of polarities, not the count.

This module does NOT (intentionally):
  - Make any LLM call. The ``goal_phrase`` field (used by the
    fingerprint step) is left NULL here; SF-103 populates it.
  - Resolve entities. ``entity_ids`` is read from the existing
    ``memory_entity_links`` join; no LLM resolution.
  - Trigger any Forge run. The dry-run CLI (SF-106) calls this
    builder explicitly; the autonomous Forge worker (Phase 1 final
    step) also calls it explicitly. Never invoked on the write path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core_api.services.outcome_inference import (
    Polarity,
    SignalEvidence,
    SignalQuery,
    contradictions,
    cross_agent_reuse,
    external_hooks,
    repeat_recall,
    supersessions,
    terminal_memory,
)

logger = logging.getLogger(__name__)


# Registry of signal modules. The order here matches the enum order
# only for log-line predictability; the dispatcher gathers
# concurrently and does not rely on it.
SIGNAL_MODULES: tuple = (
    contradictions,
    supersessions,
    repeat_recall,
    terminal_memory,
    cross_agent_reuse,
    external_hooks,
)


# Minimum weighted-evidence sum required to commit a label (vs.
# ``unknown``). Tuned conservatively: a single terminal-memory hit
# (weight 0.9) clears the bar alone; a single cross-agent-reuse hit
# (weight 0.3) does NOT. Plan §6 weighting + §17 risk #1 (garbage-in).
MIN_TOTAL_WEIGHT_FOR_LABEL: float = 0.5


@dataclass(frozen=True)
class SessionTraceRow:
    """In-memory representation of one ``session_traces`` row.

    Shape mirrors migration 021. The builder hands this dataclass
    to the upserter; the upserter renders it to the SQL bind
    parameters.
    """

    tenant_id: str
    fleet_id: str | None
    run_id: str
    agent_id: str
    outcome_label: str  # 'success' | 'failure' | 'unknown'
    memory_ids: list[str]
    entity_ids: list[str]
    signals_summary: dict[str, Any]
    started_at: datetime
    ended_at: datetime
    goal_phrase: str | None = None  # populated later by SF-103

    def as_bind(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "fleet_id": self.fleet_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "outcome_label": self.outcome_label,
            "memory_ids": self.memory_ids,
            "entity_ids": self.entity_ids,
            "signals_summary": self.signals_summary,
            "goal_phrase": self.goal_phrase,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


@dataclass
class _MemoryRow:
    """Per-memory row read from the ``memories`` scan. Kept narrow:
    the builder doesn't need ``content`` or ``embedding`` — only the
    grouping/timestamp/id fields."""

    memory_id: str
    run_id: str
    agent_id: str
    fleet_id: str | None
    created_at: datetime


# ── Public API ────────────────────────────────────────────────────


def fold_outcome_label(
    evidence: list[SignalEvidence],
    *,
    min_total_weight: float = MIN_TOTAL_WEIGHT_FOR_LABEL,
) -> str:
    """Reduce a per-trace evidence list to one of
    ``{'success', 'failure', 'unknown'}``.

    Rule:
      - SUCCESS / FAILURE polarities sum their weights independently.
      - NEUTRAL evidence is informational; it does NOT vote on the label.
      - If ``success_weight + failure_weight < min_total_weight`` →
        ``unknown`` (not enough evidence to commit).
      - If ``failure_weight > success_weight`` → ``failure``.
      - If ``success_weight > failure_weight`` → ``success``.
      - Tie (including both = 0) → ``unknown``.

    The asymmetric tie-break ("failure wins on equal totals") lives
    inside :mod:`terminal_memory` (the dominant single signal). At
    the fold layer ties go to ``unknown`` so other signals don't
    accidentally vote against the terminal-memory verdict — let the
    weights speak.
    """
    if not evidence:
        return "unknown"

    success_w = 0.0
    failure_w = 0.0
    for ev in evidence:
        if ev.polarity == Polarity.SUCCESS:
            success_w += ev.weight
        elif ev.polarity == Polarity.FAILURE:
            failure_w += ev.weight
        # NEUTRAL: no vote.

    if success_w + failure_w < min_total_weight:
        return "unknown"
    if failure_w > success_w:
        return "failure"
    if success_w > failure_w:
        return "success"
    return "unknown"


def summarize_evidence(evidence: list[SignalEvidence]) -> dict[str, Any]:
    """Render the per-trace evidence list to the JSONB shape
    persisted in ``session_traces.signals_summary``.

    Groups by signal kind so the Inbox card can render
    ``signals_summary.contradiction.firings[*]`` without re-walking
    the whole list. Order within a kind is preserved (chronological
    as the extractor returned it).
    """
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"firings": [], "total_weight": 0.0, "polarity_counts": {}}
    )
    for ev in evidence:
        slot = grouped[ev.kind.value]
        slot["firings"].append(ev.as_jsonb())
        slot["total_weight"] = round(slot["total_weight"] + ev.weight, 6)
        pc = slot["polarity_counts"]
        pc[ev.polarity.value] = pc.get(ev.polarity.value, 0) + 1
    return dict(grouped)


async def build_session_traces(
    db: Any,
    *,
    tenant_id: str,
    fleet_id: str | None,
    window_start: datetime,
    window_end: datetime,
    persist: bool = True,
) -> list[SessionTraceRow]:
    """Build session traces for the window. Returns the rows in
    memory; persists to ``session_traces`` when ``persist=True``.

    ``persist=False`` is the dry-run / eval-harness path — useful
    when calling the builder repeatedly during SF-105 fingerprint
    stability tests without mutating the DB.
    """
    query = SignalQuery(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
        run_id=None,
        agent_id=None,
    )

    # 1. Run all 6 extractors concurrently.
    extractor_results = await asyncio.gather(
        *(mod.extract(query, db) for mod in SIGNAL_MODULES),
        return_exceptions=True,
    )
    all_evidence: list[SignalEvidence] = []
    for mod, result in zip(SIGNAL_MODULES, extractor_results, strict=True):
        if isinstance(result, BaseException):
            # One extractor failing must NOT abort the whole build —
            # the others still produce useful evidence. Log + skip.
            logger.warning(
                "outcome_inference extractor %s raised %s; skipping its evidence",
                getattr(mod, "__name__", repr(mod)),
                type(result).__name__,
                exc_info=result,
            )
            continue
        all_evidence.extend(result)

    # 2. Partition evidence by (run_id, agent_id).
    #
    # Every signal extractor MUST stamp ``run_id`` and ``agent_id`` on
    # its ``SignalEvidence.details`` — without those keys the
    # evidence has no trace to attach to and is silently lost.
    # Dropping is the safe default (better than crashing the whole
    # build over one buggy extractor), but log at WARNING so a
    # regression in an extractor surfaces in the run summary rather
    # than disappearing into the void.
    by_trace: dict[tuple[str, str], list[SignalEvidence]] = defaultdict(list)
    for ev in all_evidence:
        run_id = ev.details.get("run_id")
        agent_id = ev.details.get("agent_id")
        if not (run_id and agent_id):
            logger.warning(
                "outcome_inference: dropping %s evidence — missing "
                "run_id or agent_id in details (got run_id=%r agent_id=%r)",
                ev.kind.value,
                run_id,
                agent_id,
            )
            continue
        by_trace[(run_id, agent_id)].append(ev)

    # 3. Query memories in the window to enumerate every
    #    (run_id, agent_id) group, including ones with no signal
    #    evidence (these become outcome_label='unknown').
    memories_by_trace = await _query_memories_in_window(
        db,
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
    )

    # 4. Build SessionTraceRow per trace.
    #
    # Entity resolution is hoisted OUT of the per-trace loop to avoid
    # an N+1 SQL pattern (one ``memory_entity_links`` SELECT per
    # trace). We collect every member memory id across the window
    # first, issue ONE SELECT, and partition the result back to each
    # trace inside the loop.
    all_memory_ids: list[str] = sorted(
        {m.memory_id for members in memories_by_trace.values() for m in members}
    )
    entities_by_memory = await _query_entity_ids_for_memories(db, memory_ids=all_memory_ids)

    out: list[SessionTraceRow] = []
    for (run_id, agent_id), members in memories_by_trace.items():
        evidence = by_trace.get((run_id, agent_id), [])
        label = fold_outcome_label(evidence)
        # Partition the bulk entity lookup back to this trace —
        # dedup + sort for deterministic fingerprint inputs later.
        trace_entity_ids = sorted({eid for m in members for eid in entities_by_memory.get(m.memory_id, ())})
        # Window inputs are always in the trace bounds; use the
        # actual member memory timestamps for accuracy.
        started_at = min(m.created_at for m in members)
        ended_at = max(m.created_at for m in members)
        out.append(
            SessionTraceRow(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                run_id=run_id,
                agent_id=agent_id,
                outcome_label=label,
                memory_ids=[m.memory_id for m in members],
                entity_ids=trace_entity_ids,
                signals_summary=summarize_evidence(evidence),
                started_at=started_at,
                ended_at=ended_at,
                goal_phrase=None,
            )
        )

    # 5. Persist.
    if persist and out:
        await _upsert_session_traces(db, out)

    logger.info(
        "session_trace_builder: built %d traces (success=%d failure=%d unknown=%d) "
        "for tenant=%s fleet=%s window=[%s, %s)",
        len(out),
        sum(1 for r in out if r.outcome_label == "success"),
        sum(1 for r in out if r.outcome_label == "failure"),
        sum(1 for r in out if r.outcome_label == "unknown"),
        tenant_id,
        fleet_id,
        window_start.isoformat() if window_start else None,
        window_end.isoformat() if window_end else None,
    )
    return out


# ── Private: DB I/O ───────────────────────────────────────────────


async def _query_memories_in_window(
    db: Any,
    *,
    tenant_id: str,
    fleet_id: str | None,
    window_start: datetime,
    window_end: datetime,
) -> dict[tuple[str, str], list[_MemoryRow]]:
    """Return memories grouped by ``(run_id, agent_id)``.

    Memories without a ``run_id`` are SKIPPED — they don't belong to
    any session-bounded trace and would inflate the trace count with
    one-off agent writes.
    """
    sql = """
        SELECT
            m.id           AS memory_id,
            m.run_id       AS run_id,
            m.agent_id     AS agent_id,
            m.fleet_id     AS fleet_id,
            m.created_at   AS created_at
        FROM memories AS m
        WHERE m.tenant_id = :tenant_id
          AND m.created_at >= :w_start
          AND m.created_at <  :w_end
          AND m.run_id IS NOT NULL
          AND (:fleet_id IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
        ORDER BY m.run_id, m.agent_id, m.created_at ASC
    """
    rows = (
        await db.execute(
            text(sql),
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "w_start": window_start,
                "w_end": window_end,
            },
        )
    ).fetchall()

    grouped: dict[tuple[str, str], list[_MemoryRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.run_id, row.agent_id)].append(
            _MemoryRow(
                memory_id=str(row.memory_id),
                run_id=row.run_id,
                agent_id=row.agent_id,
                fleet_id=row.fleet_id,
                created_at=row.created_at,
            )
        )
    return grouped


async def _query_entity_ids_for_memories(db: Any, *, memory_ids: list[str]) -> dict[str, list[str]]:
    """Resolve entities for a batch of memory ids via
    ``memory_entity_links``. Returns ``{memory_id: [entity_id, ...]}``
    with entity lists deduped and sorted for deterministic
    fingerprint inputs later.

    The caller passes EVERY memory id in the window in a single
    invocation; we issue ONE SQL statement and partition the result
    in Python. The previous shape (returning a flat ``list[str]``
    per call) forced a per-trace call inside the
    :func:`build_session_traces` loop, which was an N+1 against
    ``memory_entity_links`` — observable as 100+ extra round-trips
    on a busy fleet's Forge tick.

    Empty input → empty dict (no query issued). Memory ids with no
    links are absent from the returned dict (callers should default
    to ``()`` / ``[]`` on lookup).
    """
    if not memory_ids:
        return {}
    # IMPORTANT: cast the PARAMETER to uuid[], not the column to text.
    # ``memory_id::text = ANY(:memory_ids)`` would defeat any index on
    # ``memory_entity_links.memory_id`` because the column is wrapped
    # in a function call. ``CAST(:memory_ids AS uuid[])`` keeps the
    # column reference index-eligible while still letting asyncpg
    # bind the str list as text[]; PG converts text→uuid at bind time
    # as long as the strings are valid UUIDs (they are — we round-
    # tripped them via ``str(row.memory_id)`` from a uuid column
    # earlier in the build).
    sql = """
        SELECT memory_id::text AS memory_id, entity_id::text AS entity_id
        FROM memory_entity_links
        WHERE memory_id = ANY(CAST(:memory_ids AS uuid[]))
    """
    rows = (
        await db.execute(
            text(sql),
            {"memory_ids": list(memory_ids)},
        )
    ).fetchall()
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        grouped[row.memory_id].add(row.entity_id)
    return {mid: sorted(eids) for mid, eids in grouped.items()}


async def _upsert_session_traces(db: Any, rows: list[SessionTraceRow]) -> None:
    """Upsert into ``session_traces`` keyed by
    ``(tenant_id, run_id, agent_id)`` (migration 021 unique
    constraint). Re-running the builder over the same window is a
    no-op for unchanged traces and a refresh for traces whose
    evidence shifted.
    """
    if not rows:
        return

    sql = """
        INSERT INTO session_traces (
            tenant_id, fleet_id, run_id, agent_id,
            outcome_label, memory_ids, entity_ids,
            signals_summary, goal_phrase, started_at, ended_at
        )
        VALUES (
            :tenant_id, :fleet_id, :run_id, :agent_id,
            :outcome_label,
            :memory_ids::jsonb, :entity_ids::jsonb,
            :signals_summary::jsonb, :goal_phrase,
            :started_at, :ended_at
        )
        ON CONFLICT (tenant_id, run_id, agent_id) DO UPDATE SET
            fleet_id         = EXCLUDED.fleet_id,
            outcome_label    = EXCLUDED.outcome_label,
            memory_ids       = EXCLUDED.memory_ids,
            entity_ids       = EXCLUDED.entity_ids,
            signals_summary  = EXCLUDED.signals_summary,
            goal_phrase      = EXCLUDED.goal_phrase,
            started_at       = EXCLUDED.started_at,
            ended_at         = EXCLUDED.ended_at
    """
    # Bulk-execute one statement at a time (asyncpg supports
    # ``executemany`` but the bind shape with jsonb casts is cleaner
    # one-by-one and the volumes here are small — one Forge tick is
    # typically <500 traces).
    for row in rows:
        bind = row.as_bind()
        # JSONB params need to be JSON strings for asyncpg.
        bind["memory_ids"] = json.dumps(bind["memory_ids"])
        bind["entity_ids"] = json.dumps(bind["entity_ids"])
        bind["signals_summary"] = json.dumps(bind["signals_summary"])
        await db.execute(text(sql), bind)
