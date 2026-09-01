"""Repeat-recall signal — same query produced multiple recalls.

When an agent issues the same (or near-same) recall query multiple
times within a session, the first answer didn't land. Plan §6
signal #3.

**Data source — the gap this module was written against is CLOSED.**
It previously said Caura does not persist a per-recall log with the
query string preserved. That is no longer true: ``recall_event`` +
``recall_candidate`` (migration 027) shipped, and ``recall_event``
carries ``tenant_id``, ``agent_id``, ``ts``, ``source`` and
``query_text``. They are written opt-in per tenant by
``core_api.pipeline.steps.search.log_recall_event``.

The ``memory_recalls`` table this file used to name as the Phase 2
deliverable was never built and is not what shipped — do not go
looking for it. The old proxies (the agent's own memory stream; the
audit log at op='recall', which does not carry the query string) are
therefore no longer the only options.

**Phase 1 MVP**: the body still returns []. The contract is
finalised; the read is not written. Returning [] is the safe default
per plan §6 / §17 — false positives are worse than false negatives
here — and the extractor stays wired into the dispatcher so Phase
2's swap-in is body-only.

**READ THIS BEFORE WRITING THE PHASE 2 QUERY.** ``recall_candidate``
has NO ``tenant_id`` of its own and NO foreign key on ``memory_id``;
it is scoped only by the ``ON DELETE CASCADE`` from ``recall_event``.
``POST /memories/recall-log`` does not validate the candidate ids it
is handed — core-storage-api constructs each row verbatim — so a row
CAN name a memory belonging to another tenant. That is inert today
only because nothing reads the table, which stops being true the
moment this extractor does. **Any JOIN from ``recall_candidate`` to
``memories`` must carry a tenant predicate.** Joining on
``memory_id`` alone converts an unvalidated write into a cross-tenant
content read. Traced in #1180; the allowlist note on
``method:recall_log_write`` records the same requirement from the
storage side.

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

    Phase 2 implementer: the tables are already here
    (``recall_event`` / ``recall_candidate``, migration 027), so the
    only missing piece is this body. The tenant-predicate requirement
    in the module docstring is not optional — ``recall_candidate``
    rows carry unvalidated ``memory_id`` values. See #1180.
    """
    logger.info(
        "repeat_recall extractor: returning [] (Phase 1 MVP — the recall log "
        "tables exist, the Phase 2 read is not written; see module docstring). "
        "tenant=%s",
        query.tenant_id,
    )
    return []
