"""Publish ``Topics.Memory.ENRICH_REQUESTED`` events.

Convenience wrapper that resolves the tenant's enrichment provider /
model / credentials / fallback target and packs them into the
:class:`~common.events.memory_enrich_request.MemoryEnrichRequest`
payload. Mirrors :func:`common.events.memory_embed_publisher.publish_memory_embed_request`
in shape; the extra surface area is the tenant-config plumbing the
enricher needs (CAURA-595 Q1=C: tenant_config travels in the payload).

Schema-drift between this caller and
``core_worker.consumer.handle_enrich_request`` surfaces here at publish
time (Pydantic ``ValidationError``) rather than as a runtime error
inside the worker.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_enrich_request import MemoryEnrichRequest
from common.events.topics import Topics


async def publish_memory_enrich_request(
    *,
    memory_id: UUID,
    content: str,
    tenant_id: str,
    tenant_config: object | None = None,
    reference_datetime: datetime | None = None,
    agent_provided_fields: list[str] | None = None,
) -> None:
    """Publish one enrich-request event for a memory persisted without inline enrichment.

    ``tenant_config`` is a ``ResolvedConfig``-shaped object (or ``None``).
    The publisher reads the enrichment-relevant attributes off it and
    packs them into the payload so the worker doesn't need to import
    ``core_api.services.organization_settings``.

    ``agent_provided_fields`` lists the row's columns the agent set
    explicitly at write time. The worker uses it to skip overwriting
    those fields on PATCH so agent intent always wins (same gating the
    synchronous path does inline). Pass ``None`` only when the
    publisher genuinely has no idea — defaults to "trust enrichment for
    everything", which is the current pre-PR-C behaviour.

    Fire-and-forget: ``bus.publish`` returns once the transport has
    accepted the message (Pub/Sub) or the in-process subscriber has been
    invoked. Callers wrap the call in
    ``track_task(tracked_task(...))`` so a transport error doesn't bubble
    out of the request handler.
    """
    fallback_provider: str | None = None
    fallback_model: str | None = None
    if tenant_config is not None and hasattr(tenant_config, "resolve_fallback"):
        # ``resolve_fallback()`` reads the resolver's own attribute
        # tuples — only ``AttributeError`` / ``KeyError`` / ``TypeError``
        # are plausible. Anything else is a real bug; let it surface
        # to the caller's ``track_task`` wrapper instead of silently
        # demoting to "no fallback".
        try:
            fallback_provider, fallback_model = tenant_config.resolve_fallback()
        except (AttributeError, KeyError, TypeError):
            fallback_provider, fallback_model = None, None

    payload = MemoryEnrichRequest(
        memory_id=memory_id,
        tenant_id=tenant_id,
        content=content,
        reference_datetime=(
            reference_datetime.isoformat() if reference_datetime else None
        ),
        enrichment_provider=getattr(tenant_config, "enrichment_provider", None),
        enrichment_model=getattr(tenant_config, "enrichment_model", None),
        openai_api_key=getattr(tenant_config, "openai_api_key", None),
        anthropic_api_key=getattr(tenant_config, "anthropic_api_key", None),
        openrouter_api_key=getattr(tenant_config, "openrouter_api_key", None),
        gemini_api_key=getattr(tenant_config, "gemini_api_key", None),
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
        agent_provided_fields=agent_provided_fields,
    )
    event = Event(
        event_type=Topics.Memory.ENRICH_REQUESTED,
        tenant_id=tenant_id,
        # ``include_secrets=True`` opts out of the
        # ``MemoryEnrichRequest._redact_secrets`` field-serializer so
        # the wire payload carries the raw API keys (the worker needs
        # them to resolve the tenant's LLM provider). Every other
        # ``model_dump()`` call site — logging, audit, ad-hoc
        # ``json.dumps`` — gets ``"***"`` for those fields.
        payload=payload.model_dump(mode="json", context={"include_secrets": True}),
    )
    bus = get_event_bus()
    await bus.publish(Topics.Memory.ENRICH_REQUESTED, event)
