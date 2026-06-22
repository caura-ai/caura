"""Supersession signal — directly superseded memories indicate failure.

A memory with ``supersedes_id IS NOT NULL`` is the NEWER one that
replaces an older statement. The OLDER memory (referenced by
``supersedes_id``) was, by definition, wrong or stale. The signal
fires on the OLDER memory's trace, marking that trace as a likely
failure.

Worked example: agent A writes "Deploy script v4 is fine in eu-west"
(memory M1). Later, agent A or B writes "Deploy script v4 hangs in
eu-west on step 7" (memory M2, with ``supersedes_id=M1``). The
supersession signal:

  - fires polarity=FAILURE on M1's session-trace (M1 was wrong)
  - does NOT fire on M2's session-trace (M2 is the corrective)

This avoids the trap where a session that DISCOVERED a contradiction
gets penalised for it — only the original incorrect session pays.

Plan §6 signal #2. Plan §3 schema field
``signals_summary.supersession``.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client

from . import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
    SignalQuery,
    parse_observed_at,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.SUPERSESSION


async def extract(query: SignalQuery) -> list[SignalEvidence]:
    """Find memories superseded WITHIN this window's trace.

    Emits one ``SignalEvidence`` per superseded (older) memory, keyed at
    the SUPERSEDED memory's ``(run_id, agent_id)`` — so the FAILURE
    polarity lands on the session that wrote the bad memory, not the
    session that found the contradiction.

    Window semantics: the ``new`` memory's ``created_at`` drives the window
    check (when the contradiction surfaced), so a memory written weeks ago
    still accumulates failure evidence retroactively if superseded inside
    the current scan window.

    As of Fix 2 Ph5a the analytic read goes through core-storage-api
    (``sc.outcome_supersession_signals``); the self-join on
    ``supersedes_id`` + the cross-fleet isolation predicate on the OLD
    memory live in ``PostgresService.outcome_supersession_signals``.
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.SUPERSESSION]

    rows = await get_storage_client().outcome_supersession_signals(
        tenant_id=query.tenant_id,
        fleet_id=query.fleet_id,
        window_start=query.window_start,
        window_end=query.window_end,
        run_id=query.run_id,
        agent_id=query.agent_id,
    )

    out: list[SignalEvidence] = []
    for row in rows:
        out.append(
            SignalEvidence(
                kind=SignalKind.SUPERSESSION,
                polarity=Polarity.FAILURE,
                weight=weight,
                memory_ids=(str(row["superseded_id"]),),
                details={
                    "superseded_memory_id": str(row["superseded_id"]),
                    "superseded_by_memory_id": str(row["by_id"]),
                    "run_id": row["run_id"],
                    "agent_id": row["agent_id"],
                },
                observed_at=parse_observed_at(row.get("observed_at")),
            )
        )

    if out:
        logger.debug(
            "supersession signal: %d firings in window for tenant=%s",
            len(out),
            query.tenant_id,
        )
    return out
