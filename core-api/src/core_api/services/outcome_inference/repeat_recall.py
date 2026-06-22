"""Repeat-recall signal — same query produced multiple recalls.

When an agent issues the same (or near-same) recall query multiple
times within a session, the first answer didn't land. Plan §6
signal #3.

**Data source gap (acknowledged)**: MemClaw does not currently
persist a per-recall log keyed by (session, agent, query, ts) with
the query string preserved. Two reasonable proxies are available
today:

  (a) The agent's own memory stream — if the same trace produced
      multiple recalls *and* multiple writes about the same entity,
      the entity sequence within a single run_id reveals the
      pattern.
  (b) The audit log (op='recall') — but does not carry the query
      string.

**Phase 1 MVP**: ship the extractor with the contract finalised but
the body conservative — return [] unless the explicit recall log
(``memory_recalls`` table, Phase 2 deliverable) is in place. This
prevents false positives from the proxy approach while keeping the
extractor wired into the dispatcher so Phase 2's swap-in is body-
only.

The extractor logs at INFO level on every invocation so operators
running Phase 1 forge dry-runs can see the gap explicitly in the
console without it being silent.
"""

from __future__ import annotations

import logging

from . import (
    DEFAULT_SIGNAL_WEIGHTS,  # noqa: F401  (re-exported via __init__; kept here for consistency)
    SignalEvidence,
    SignalKind,
    SignalQuery,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.REPEAT_RECALL


async def extract(query: SignalQuery) -> list[SignalEvidence]:
    """Phase 1 MVP returns []. Phase 2 swaps in the recall-log read.

    Returning [] is the safe default per plan §6 / plan §17 risk
    table — false positives are worse than false negatives for
    outcome inference (they'd label good traces as failures and
    push Forge to NOT propose what was actually a fine procedure).
    """
    logger.info(
        "repeat_recall extractor: returning [] (Phase 1 MVP — recall log "
        "table arrives in Phase 2; see module docstring). tenant=%s",
        query.tenant_id,
    )
    return []
