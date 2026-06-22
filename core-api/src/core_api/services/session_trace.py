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
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core_api.clients.storage_client import get_storage_client
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
    *,
    tenant_id: str,
    fleet_id: str | None,
    window_start: datetime,
    window_end: datetime,
    persist: bool = True,
) -> list[SessionTraceRow]:
    """Build session traces for the window. Returns the rows in
    memory; persists to ``session_traces`` when ``persist=True``.

    As of Fix 2 Ph5a every read/write goes through core-storage-api (the
    extractors, the memory-window + entity-links reads, and the upsert);
    this function no longer takes a DB session.

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
        *(mod.extract(query) for mod in SIGNAL_MODULES),
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
    entities_by_memory = await _query_entity_ids_for_memories(tenant_id=tenant_id, memory_ids=all_memory_ids)

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
        await _upsert_session_traces(tenant_id, out)

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
    *,
    tenant_id: str,
    fleet_id: str | None,
    window_start: datetime,
    window_end: datetime,
) -> dict[tuple[str, str], list[_MemoryRow]]:
    """Return memories grouped by ``(run_id, agent_id)`` via core-storage-api.

    Memories without a ``run_id`` are SKIPPED storage-side — they don't
    belong to any session-bounded trace and would inflate the trace count
    with one-off agent writes. The PG SQL lives in
    ``PostgresService.session_memories_in_window``.
    """
    sc = get_storage_client()
    rows = await sc.session_memories_in_window(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
    )

    grouped: dict[tuple[str, str], list[_MemoryRow]] = defaultdict(list)
    for row in rows:
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        grouped[(row["run_id"], row["agent_id"])].append(
            _MemoryRow(
                memory_id=str(row["memory_id"]),
                run_id=row["run_id"],
                agent_id=row["agent_id"],
                fleet_id=row.get("fleet_id"),
                created_at=created_at,
            )
        )
    return grouped


async def _query_entity_ids_for_memories(*, tenant_id: str, memory_ids: list[str]) -> dict[str, list[str]]:
    """Resolve entities for a batch of memory ids via core-storage-api.
    Returns ``{memory_id: [entity_id, ...]}`` with entity lists deduped and
    sorted for deterministic fingerprint inputs later.

    The caller passes EVERY memory id in the window in a single
    invocation; storage issues ONE SQL statement (the param-cast to
    ``uuid[]`` keeps the index eligible — see
    ``PostgresService.memory_entity_links_batch``) and we partition the
    result in Python.

    Empty input → empty dict (no call issued). Memory ids with no links are
    absent from the returned dict (callers default to ``()`` / ``[]``).
    """
    if not memory_ids:
        return {}
    sc = get_storage_client()
    rows = await sc.session_trace_entity_links(tenant_id=tenant_id, memory_ids=list(memory_ids))
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        grouped[row["memory_id"]].add(row["entity_id"])
    return {mid: sorted(eids) for mid, eids in grouped.items()}


async def _upsert_session_traces(tenant_id: str, rows: list[SessionTraceRow]) -> None:
    """Upsert into ``session_traces`` keyed by
    ``(tenant_id, run_id, agent_id)`` (migration 021 unique constraint) via
    core-storage-api. Re-running the builder over the same window is a no-op
    for unchanged traces and a refresh for traces whose evidence shifted.

    The ``INSERT ... ON CONFLICT ... DO UPDATE`` + jsonb casts live in
    ``PostgresService.session_traces_upsert``; we hand it a batch of bind
    dicts (datetimes → ISO strings for the JSON wire; jsonb fields as python
    objects that storage json-dumps).
    """
    if not rows:
        return
    sc = get_storage_client()

    def _iso(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    traces: list[dict[str, Any]] = []
    for row in rows:
        bind = row.as_bind()
        bind["started_at"] = _iso(bind["started_at"])
        bind["ended_at"] = _iso(bind["ended_at"])
        # ``tenant_id`` is forced by the batch param storage-side; drop the
        # per-row copy so the wire payload matches the endpoint contract.
        # Assert it matched the batch tenant first — belt-and-braces against a
        # smuggled cross-tenant row (storage also enforces the batch tenant).
        assert bind.get("tenant_id") in (None, tenant_id), "per-row tenant_id must match batch tenant_id"
        bind.pop("tenant_id", None)
        traces.append(bind)
    await sc.upsert_session_traces(tenant_id=tenant_id, traces=traces)
