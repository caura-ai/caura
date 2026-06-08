"""Outcome inference — the 6 free signals (Skill Factory plan §6).

The lake produces a memory stream. This package mines that stream
for passive outcome evidence — no ``memclaw_evolve`` call required.
Each signal module reads existing MemClaw data (memories table
columns, recall counters, contradiction status, audit log) and
emits :class:`SignalEvidence` events keyed by ``(tenant_id, run_id,
agent_id)``. The session-trace builder (SF-102) consumes these
events and assigns a single ``outcome_label`` per trace.

Signal modules (one file each):

  1. :mod:`.contradictions`     — recalled memory was contradicted soon
                                  after  (existing ``contradiction_detector``
                                  emits ``status='outdated' | 'conflicted'``)
  2. :mod:`.supersessions`      — memory was directly superseded (``memories.supersedes_id``)
  3. :mod:`.repeat_recall`      — same query produced multiple recalls; first answer didn't land
  4. :mod:`.terminal_memory`    — last memory in session = ``shipped`` / ``fixed`` (success)
                                  vs ``blocked`` / ``abandoned`` (failure)
  5. :mod:`.cross_agent_reuse`  — ≥3 distinct agents recalled the same memory ⇒ load-bearing
  6. :mod:`.external_hooks`     — git / PR / CI status tied to ``run_id`` (when available)

All signal modules conform to the :class:`SignalExtractor` protocol.
The session-trace builder runs them concurrently and folds their
results into per-trace evidence.

This module is INTENTIONALLY zero-cost on the write path. All
extractors run lazily, on demand, when Forge or the session-trace
builder asks for evidence. No producer hooks, no on-write
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class SignalKind(str, Enum):
    """Stable identifiers for the 6 signals. Used as dict keys in
    :attr:`SessionTrace.signals_summary` and as discriminators in
    log lines / metrics. NEVER renumber; the value strings land in
    persisted ``session_traces.signals_summary`` JSONB rows.
    """

    CONTRADICTION = "contradiction"
    SUPERSESSION = "supersession"
    REPEAT_RECALL = "repeat_recall"
    TERMINAL_MEMORY = "terminal_memory"
    CROSS_AGENT_REUSE = "cross_agent_reuse"
    EXTERNAL_HOOK = "external_hook"


# Outcome polarity each signal contributes. Used by the session-trace
# builder to decide the final ``outcome_label``: it sums weighted
# polarities across all signals that fired for a given trace.
class Polarity(str, Enum):
    SUCCESS = "success"  # signal evidence supports a successful outcome
    FAILURE = "failure"  # signal evidence supports a failed outcome
    NEUTRAL = "neutral"  # informational; no polarity (e.g. cross-agent-reuse)


@dataclass(frozen=True)
class SignalEvidence:
    """One signal firing.

    Aggregated per ``(tenant_id, run_id, agent_id)`` by the session-
    trace builder. The fields below are the MINIMUM the builder needs;
    each signal may set ``details`` to a dict carrying extractor-
    specific context (e.g. ``{"contradicted_memory_id": "...",
    "by_memory_id": "..."}``) which the inbox-card UI can surface.
    """

    kind: SignalKind
    polarity: Polarity
    # Weight is a per-signal strength in [0.0, 1.0]. The builder
    # multiplies polarity by weight when aggregating. Strong signals
    # (terminal memory) carry weight near 1.0; weak signals
    # (cross-agent reuse) closer to 0.3.
    weight: float
    # Pointers back into the lake for traceability in the inbox card.
    memory_ids: tuple[str, ...] = ()
    # Free-shape extractor context; lands in
    # session_traces.signals_summary[<kind>].details.
    details: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None

    def as_jsonb(self) -> dict:
        """Render to the shape persisted in ``session_traces.signals_summary``."""
        return {
            "polarity": self.polarity.value,
            "weight": self.weight,
            "memory_ids": list(self.memory_ids),
            "details": self.details,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


# ── Default per-signal weight floors ──────────────────────────────
#
# Tuned so a single strong signal (terminal "shipped") is enough to
# label a trace; weaker signals (cross-agent reuse alone) are NOT.
# The session-trace builder applies these as priors; per-tenant
# tuning may override via org settings later (Phase 4+).
DEFAULT_SIGNAL_WEIGHTS: dict[SignalKind, float] = {
    SignalKind.TERMINAL_MEMORY: 0.9,
    SignalKind.EXTERNAL_HOOK: 0.85,
    SignalKind.CONTRADICTION: 0.7,
    SignalKind.SUPERSESSION: 0.7,
    SignalKind.REPEAT_RECALL: 0.4,
    SignalKind.CROSS_AGENT_REUSE: 0.3,
}


@dataclass(frozen=True)
class SignalQuery:
    """Inputs every extractor accepts.

    Extractors are pure functions of ``(query, db)``. They return a
    list of :class:`SignalEvidence`. The builder calls all 6
    concurrently for one trace window, then folds the results.
    """

    tenant_id: str
    fleet_id: str | None
    # Window the trace covers. Extractors return only evidence
    # observed within this window (exclusive of ``window_end``).
    window_start: datetime
    window_end: datetime
    # If set, restrict to this single trace's run_id / agent_id.
    # If None, the extractor returns evidence across all traces in
    # the window — the builder partitions by (run_id, agent_id).
    run_id: str | None = None
    agent_id: str | None = None


@runtime_checkable
class SignalExtractor(Protocol):
    """Contract every signal extractor implements.

    A concrete extractor is a callable + a ``kind`` attribute. The
    session-trace builder discovers extractors by importing each
    submodule in this package and collecting any module-level
    ``EXTRACTOR`` object exposing this Protocol.
    """

    kind: SignalKind

    async def extract(self, query: SignalQuery, db: Any) -> list[SignalEvidence]: ...


# Public re-exports.
__all__ = [
    "DEFAULT_SIGNAL_WEIGHTS",
    "Polarity",
    "SignalEvidence",
    "SignalExtractor",
    "SignalKind",
    "SignalQuery",
]
