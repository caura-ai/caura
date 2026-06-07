"""Terminal-memory signal — the last memory in a session labels it.

The end of a session is the strongest free signal we have. A trace
ending with a memory whose content matches "shipped" / "done" /
"fixed" / "deployed" is almost certainly a successful trace; a trace
ending with "blocked" / "abandoning" / "stuck" / "rolled back" is
almost certainly a failure.

Plan §6 signal #4. Plan default weight 0.9 — the highest of the six
because it is the closest thing to ground truth available without
hooking external systems.

**MVP approach**: keyword/regex classifier on the LAST memory of each
(tenant, fleet, run, agent) window. Per-trace, not per-memory — so
one evidence per session, not per matching word.

The classifier is intentionally lenient on the SUCCESS side and
strict on the FAILURE side: false-positive successes are far more
expensive (we'd promote a bad procedure to a skill) than false-
negative failures (we'd just leave a trace unlabeled). Hence the
asymmetric thresholds + curated keyword lists below.

A Phase-2+ upgrade can swap the regex classifier for an LLM call
without changing the extractor signature — same return shape, same
caller contract. Documented as such in :data:`CLASSIFIER_VERSION`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text

from . import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
    SignalQuery,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.TERMINAL_MEMORY

# Bump this when the classifier changes (e.g. swapping in an LLM).
# Stored in evidence.details so re-running Forge against the same
# data can detect classifier drift via re-eval.
CLASSIFIER_VERSION: str = "v1-regex"


# Curated keyword sets. Word boundaries on both sides so "shipped" in
# "shipped/abandoned" doesn't accidentally fire success. Lowercased
# match (the content is lowercased before regex).
_SUCCESS_TERMS: tuple[str, ...] = (
    r"shipped",
    r"deployed",
    r"merged",
    r"fixed",
    r"resolved",
    r"closed",
    r"completed",
    r"done",
    r"landed",
    r"passing",
    r"verified",
    r"approved",
    r"signed off",
)
_FAILURE_TERMS: tuple[str, ...] = (
    r"blocked",
    r"abandoned",
    r"abandoning",
    r"stuck",
    r"rolled back",
    r"reverted",
    r"giving up",
    r"gave up",
    r"can't",
    r"could not",
    r"failed to",
    r"unable to",
    r"timeout",
    r"crashed",
)
# Compile once. ``\b`` works for word boundaries; we accept some
# adjacent punctuation (apostrophes in "can't") via the curated list.
_SUCCESS_RE = re.compile(r"\b(?:" + "|".join(_SUCCESS_TERMS) + r")\b", re.IGNORECASE)
_FAILURE_RE = re.compile(r"\b(?:" + "|".join(_FAILURE_TERMS) + r")\b", re.IGNORECASE)


def _classify(content: str | None) -> Polarity | None:
    """Return SUCCESS / FAILURE if confident, None if ambiguous.

    Asymmetric rules:
      - FAILURE always wins ties (better to under-promote skills than to
        promote bad ones).
      - At least one keyword required to commit a label. Empty content
        ⇒ unlabeled.
    """
    if not content:
        return None
    has_fail = bool(_FAILURE_RE.search(content))
    has_succ = bool(_SUCCESS_RE.search(content))
    if has_fail:
        return Polarity.FAILURE
    if has_succ:
        return Polarity.SUCCESS
    return None


async def extract(query: SignalQuery, db: Any) -> list[SignalEvidence]:
    """Find the LAST memory of each session within the window and
    classify its content.

    Window: a memory qualifies as "terminal" if its ``created_at`` is
    the maximum across its (run_id, agent_id) group AND falls inside
    [window_start, window_end). The window-trailing semantics
    elegantly skip mid-session memories that happen to contain the
    word "shipped" but aren't the terminal write.

    Output: at most one ``SignalEvidence`` per (run_id, agent_id) pair
    that produced a confidently classified terminal.
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.TERMINAL_MEMORY]

    # DISTINCT ON pattern picks the latest memory per (run_id,
    # agent_id) — Postgres-specific but already used elsewhere in
    # the codebase (see contradictions.py).
    sql = """
        SELECT DISTINCT ON (m.run_id, m.agent_id)
            m.id          AS memory_id,
            m.run_id      AS run_id,
            m.agent_id    AS agent_id,
            m.content     AS content,
            m.created_at  AS observed_at
        FROM memories AS m
        WHERE m.tenant_id = :tenant_id
          AND m.created_at >= :w_start
          AND m.created_at <  :w_end
          AND m.run_id IS NOT NULL
          AND (:fleet_id IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
          AND (:run_id   IS NULL OR m.run_id   = :run_id)
          AND (:agent_id IS NULL OR m.agent_id = :agent_id)
        ORDER BY m.run_id, m.agent_id, m.created_at DESC
    """

    rows = (
        await db.execute(
            text(sql),
            {
                "tenant_id": query.tenant_id,
                "fleet_id": query.fleet_id,
                "w_start": query.window_start,
                "w_end": query.window_end,
                "run_id": query.run_id,
                "agent_id": query.agent_id,
            },
        )
    ).fetchall()

    out: list[SignalEvidence] = []
    classified_count = 0
    for row in rows:
        verdict = _classify(row.content)
        if verdict is None:
            continue
        classified_count += 1
        out.append(
            SignalEvidence(
                kind=SignalKind.TERMINAL_MEMORY,
                polarity=verdict,
                weight=weight,
                memory_ids=(str(row.memory_id),),
                details={
                    "memory_id": str(row.memory_id),
                    "run_id": row.run_id,
                    "agent_id": row.agent_id,
                    "classifier_version": CLASSIFIER_VERSION,
                    "verdict": verdict.value,
                },
                observed_at=row.observed_at,
            )
        )

    if rows:
        logger.debug(
            "terminal_memory signal: %d/%d terminals classified for tenant=%s",
            classified_count,
            len(rows),
            query.tenant_id,
        )
    return out
