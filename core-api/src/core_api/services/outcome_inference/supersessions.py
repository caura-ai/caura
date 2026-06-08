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

kind: SignalKind = SignalKind.SUPERSESSION


async def extract(query: SignalQuery, db: Any) -> list[SignalEvidence]:
    """Find memories superseded WITHIN this window's trace.

    The query returns one row per superseded (older) memory: it joins
    ``memories AS new`` (the corrective) against ``memories AS old``
    (the one being superseded). We emit one ``SignalEvidence`` per
    superseded memory, keyed at the SUPERSEDED memory's
    ``(run_id, agent_id)`` — so the FAILURE polarity lands on the
    session that wrote the bad memory, not the session that found the
    contradiction.

    Window semantics: we use the ``new`` memory's ``created_at`` for
    the window check (when the contradiction surfaced), not the
    ``old`` memory's. This lets a memory written weeks ago still
    accumulate failure evidence retroactively if it gets superseded
    inside the current scan window.
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.SUPERSESSION]

    sql = """
        SELECT
            old_mem.id           AS superseded_id,
            old_mem.run_id       AS run_id,
            old_mem.agent_id     AS agent_id,
            new_mem.id           AS by_id,
            new_mem.created_at   AS observed_at
        FROM memories AS new_mem
        JOIN memories AS old_mem
          ON old_mem.id = new_mem.supersedes_id
         AND old_mem.tenant_id = new_mem.tenant_id
        WHERE new_mem.tenant_id = :tenant_id
          AND new_mem.created_at >= :w_start
          AND new_mem.created_at <  :w_end
          AND (:fleet_id  IS NULL OR new_mem.fleet_id  = :fleet_id  OR new_mem.fleet_id IS NULL)
          -- Cross-fleet isolation: the FAILURE polarity must land on
          -- a trace inside the queried fleet, not just the corrective
          -- memory that triggered the supersession. Without this, an
          -- agent in fleet A correcting a fact written by fleet B
          -- would erroneously land failure evidence on fleet B's
          -- trace inside a fleet-A scan.
          AND (:fleet_id  IS NULL OR old_mem.fleet_id  = :fleet_id  OR old_mem.fleet_id IS NULL)
          AND (:run_id    IS NULL OR old_mem.run_id    = :run_id)
          AND (:agent_id  IS NULL OR old_mem.agent_id  = :agent_id)
          AND old_mem.run_id IS NOT NULL
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
    for row in rows:
        out.append(
            SignalEvidence(
                kind=SignalKind.SUPERSESSION,
                polarity=Polarity.FAILURE,
                weight=weight,
                memory_ids=(str(row.superseded_id),),
                details={
                    "superseded_memory_id": str(row.superseded_id),
                    "superseded_by_memory_id": str(row.by_id),
                    "run_id": row.run_id,
                    "agent_id": row.agent_id,
                },
                observed_at=row.observed_at,
            )
        )

    if out:
        logger.debug(
            "supersession signal: %d firings in window for tenant=%s",
            len(out),
            query.tenant_id,
        )
    return out
