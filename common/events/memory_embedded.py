"""Typed payload for the ``memclaw.memory.embedded`` topic.

Counterpart to :class:`~common.events.memory_embed_request.MemoryEmbedRequest`:
core-worker emits one of these *after* successfully PATCHing an
embedding onto the memory row. core-api subscribes for downstream side
effects — the post-embed contradiction-detection hook, which closes the
known gap where ``handle_memory_enriched`` would silently defer when
enrichment finished before embedding (the only case hit when
``EMBED_ON_HOT_PATH=false``).

Carries the minimum the back-channel consumer needs: enough to reach
the right row plus the content the detector compares against. The
embedding itself is *not* shipped — it's already persisted, the
consumer reads storage to recover it.

The envelope itself is :class:`common.events.base.Event`; this is just
the shape of its ``payload`` dict.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryEmbedded(BaseModel):
    """Announce a successful embed of a memory row."""

    # ``extra="ignore"`` for the same rolling-deploy reason as
    # :class:`MemoryEnriched`: this is a cross-service consumer schema
    # (core-api subscribes to what core-worker publishes). ``forbid``
    # would silently ack-drop additive fields during a worker-first
    # deploy. The publisher-side construction is the strict-validation
    # boundary.
    model_config = ConfigDict(frozen=True, extra="ignore")

    memory_id: UUID
    tenant_id: str
    # Carried so the contradiction-detection consumer doesn't need a
    # separate storage GET just to feed the detector. Same trade
    # MemoryEmbedRequest.content and MemoryEnriched.content already make.
    content: str = Field(min_length=1)
