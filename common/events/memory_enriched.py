"""Typed payload for the ``memclaw.memory.enriched`` topic (CAURA-595).

Counterpart to :class:`~common.events.memory_enrich_request.MemoryEnrichRequest`:
core-worker emits one of these *after* successfully PATCHing enrichment
fields onto the memory row. core-api subscribes for downstream side
effects — primarily a hint-driven re-embed when the LLM produced a
``retrieval_hint`` that would beat the raw-content embedding's
recall.

Carries the minimum the back-channel consumer needs: enough to reach the
right row plus the hint (already validated/trimmed by
``_validate_enrichment``). The full ``EnrichmentResult`` is *not*
shipped — it's already persisted, anyone who needs it can read storage.

Empty / missing ``retrieval_hint`` is a signal that no re-embed is
warranted (heuristic-fallback enrichments, content-already-query-aligned
inputs).

The envelope itself is :class:`common.events.base.Event`; this is just
the shape of its ``payload`` dict.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryEnriched(BaseModel):
    """Announce a successful enrichment of a memory row."""

    # ``extra="ignore"`` for the same rolling-deploy reason as
    # ``MemoryEnrichRequest``: this is a cross-service consumer schema
    # (core-api subscribes to what core-worker publishes). With
    # ``forbid``, a worker-shipped-first additive field would fail
    # validation in core-api during the deploy window and silently
    # ack-drop every back-channel message until core-api updates. The
    # publisher-side construction is the strict-validation boundary.
    model_config = ConfigDict(frozen=True, extra="ignore")

    memory_id: UUID
    tenant_id: str
    # Carried so the back-channel consumer doesn't need a storage GET
    # round trip just to dispatch contradiction detection. Same trade
    # we made for ``MemoryEmbedRequest.content``.
    content: str = Field(min_length=1)
    # Empty string when the enricher produced no useful hint —
    # back-channel consumers should short-circuit on falsy.
    retrieval_hint: str = ""
