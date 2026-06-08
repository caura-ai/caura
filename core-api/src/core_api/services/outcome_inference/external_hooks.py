"""External hooks signal — git/PR/CI status tied to ``run_id``.

The ground-truth-iest signal we have when available: a PR merged
within the window with a ``run_id`` matching the trace is a near-
certain SUCCESS; a CI run that failed with the same ``run_id`` is
a near-certain FAILURE. Plan §6 signal #6, weight 0.85.

**MVP**: the OSS MemClaw deployment has no built-in hook to ingest
external events from git / GitHub / CI today. The extractor ships
with the contract finalised but the body returns [] until an
optional ``external_hooks`` table or webhook ingest is wired.

The Phase-5 OpenClaw bridge integration is the natural place to
ingest these — OpenClaw plugins surface workflow-completion events
that already carry a ``run_id`` correlatable to MemClaw memory
writes. When that lands, this extractor becomes a SELECT against
the ingested-events table.

**Why ship the stub now**: keeping the extractor in the signal
registry from day one means the session-trace builder doesn't have
to learn about a new signal type later; only the extractor body
changes. Same contract, same return shape.
"""

from __future__ import annotations

import logging
from typing import Any

from . import (
    DEFAULT_SIGNAL_WEIGHTS,  # noqa: F401
    SignalEvidence,
    SignalKind,
    SignalQuery,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.EXTERNAL_HOOK


async def extract(query: SignalQuery, db: Any) -> list[SignalEvidence]:
    """Phase 1 MVP returns []. Enterprise / Phase 5 swaps in the
    real ingest-events read against an ``external_hooks_events``
    table (or equivalent) populated by webhook handlers.
    """
    logger.debug(
        "external_hooks extractor: returning [] (no ingest configured "
        "in MVP — Phase 5 OpenClaw bridge / webhook). tenant=%s",
        query.tenant_id,
    )
    return []
