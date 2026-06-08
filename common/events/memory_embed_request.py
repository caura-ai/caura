"""Typed payload for the ``memclaw.memory.embed-requested`` topic.

The publisher (core-api, write hot path under CAURA-594 full form) emits
one of these per memory written with ``embedding=NULL``. The consumer
(core-worker) validates the envelope's ``payload`` against this model
before invoking the embedding provider — schema-drift between publisher
and consumer surfaces as a Pydantic ``ValidationError`` instead of a
runtime KeyError deep inside the worker.

The envelope itself is :class:`common.events.base.Event`; this is just
the shape of its ``payload`` dict.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryEmbedRequest(BaseModel):
    """Request to compute and persist an embedding for a memory row.

    Fields:
        memory_id: id of the row in ``memories`` to backfill.
        tenant_id: tenant scope — passed through to the storage PATCH
            so the storage-side guard rejects cross-tenant updates.
        content: text to embed. Carried in the event rather than fetched
            from storage so the worker doesn't need a read round-trip
            on the hot path.
        content_hash: optional dedup key for the content-hash embedding
            cache lookup before calling the provider. Skip the call if
            the hash is already embedded for this tenant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: UUID
    tenant_id: str
    content: str = Field(min_length=1)
    # ``min_length=1`` so an empty-string hash from a buggy publisher
    # is rejected at validation time rather than silently skipping the
    # cache lookup downstream (the consumer's ``if request.content_hash:``
    # treats ``""`` and ``None`` the same — explicit is better here).
    content_hash: str | None = Field(default=None, min_length=1)
