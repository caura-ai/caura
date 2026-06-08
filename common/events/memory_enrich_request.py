"""Typed payload for the ``memclaw.memory.enrich-requested`` topic.

Publisher (core-api, write hot path under CAURA-595) emits one of these
per memory written without inline enrichment. Consumer (core-worker)
validates the envelope's ``payload`` against this model before invoking
``common.enrichment.enrich_memory`` — schema drift between publisher and
consumer surfaces as a Pydantic ``ValidationError`` instead of a runtime
``KeyError`` deep inside the worker.

Per the locked design (CAURA-595 Q1=C), the publisher resolves the
tenant's enrichment provider/model/keys and bakes them into the payload
so the worker stays stateless w.r.t. tenants — no DB read, no
``ResolvedConfig`` import, no shared cache. The price is sensitive
material on the wire: keys live in encrypted Pub/Sub at rest and in
flight, but anyone with subscription read access can see them. Same
exposure surface as core-api's settings/logs.

Fallback resolution moves to publish time too — the publisher calls its
own ``ResolvedConfig.resolve_fallback()`` and ships the resolved tuple
as ``fallback_provider`` / ``fallback_model``. The worker reconstructs
a duck-typed config object that exposes the fields
``common.enrichment.service.enrich_memory`` reads, plus a synthesised
``resolve_fallback()`` callable.

The envelope itself is :class:`common.events.base.Event`; this is just
the shape of its ``payload`` dict.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SerializationInfo, field_serializer


class MemoryEnrichRequest(BaseModel):
    """Request to compute and persist enrichment for a memory row.

    Fields mirror :class:`~common.events.memory_embed_request.MemoryEmbedRequest`
    where they overlap (``memory_id``, ``tenant_id``, ``content``) and add
    the resolved tenant config the enricher needs.

    All credential / config fields are ``None`` when the tenant doesn't
    set them; the worker falls through to the platform-LLM singleton or
    the keyword heuristic in that case (matching the existing 3-tier
    ``get_llm_provider`` resolution).
    """

    # ``extra="ignore"`` rather than ``"forbid"``: this is a Pub/Sub
    # *consumer* schema. With ``forbid`` a publisher-shipped-first
    # additive field (any new optional payload key) would fail
    # validation in the worker and silently ack-drop every in-flight
    # message during the deploy window. The publisher's call site is
    # the strict-validation boundary — it constructs ``MemoryEnrichRequest``
    # with its own known fields and a typo there is a real bug. Match
    # what's already correct for the sync embed flow at the wire
    # boundary.
    model_config = ConfigDict(frozen=True, extra="ignore")

    memory_id: UUID
    tenant_id: str
    content: str = Field(min_length=1)

    # Reference datetime used as ``today`` in the prompt's
    # ``ts_valid_*`` extraction guidance. Worker defaults to ``date.today()``
    # when omitted; sending it explicitly pins the value to the publisher's
    # clock so a delayed delivery doesn't drift the prompt.
    reference_datetime: str | None = None

    # Provider selection — mirrors the ``ResolvedConfig`` attributes
    # ``common.enrichment.service.enrich_memory`` reads.
    enrichment_provider: str | None = None
    enrichment_model: str | None = None

    # Per-tenant credentials. Only the keys the tenant has configured
    # are populated; the rest stay ``None`` and the worker resolves
    # them via the standard fallback chain (platform singleton → fake).
    #
    # SECURITY: defence-in-depth applies in two places:
    #   1. ``repr=False`` (Pydantic ``Field`` config below) keeps an
    #      accidental ``logger.info("got %s", request)`` from leaking
    #      the secret via Python's ``repr()``.
    #   2. ``_redact_secrets`` (the field_serializer below) redacts
    #      these fields in *every* ``model_dump()`` call by default.
    #      The publisher passes ``context={"include_secrets": True}``
    #      on the wire-format dump so Pub/Sub still sees the raw
    #      values; any other code path (logs, audit dumps, debugging
    #      tools that ``json.dumps()`` the model) gets ``"***"``.
    # Wire-format keys still travel in the Pub/Sub message body — the
    # CAURA-595 design (Q1=C) accepts this in exchange for a
    # stateless worker; the wire-level mitigation is IAM (minimum
    # subscriber bindings on ``memclaw.memory.enrich-requested``, no
    # wildcard read grants on the topic, no audit-log mining of
    # payloads). PR-D's bootstrap script enforces those grants.
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    openrouter_api_key: str | None = Field(default=None, repr=False)
    gemini_api_key: str | None = Field(default=None, repr=False)

    @field_serializer(
        "openai_api_key",
        "anthropic_api_key",
        "openrouter_api_key",
        "gemini_api_key",
        when_used="always",
    )
    def _redact_secrets(
        self, value: str | None, info: SerializationInfo
    ) -> str | None:
        """Redact API keys from ``model_dump()`` unless explicitly opted-in.

        Pub/Sub publish path passes
        ``context={"include_secrets": True}`` so the wire payload
        still carries the raw values; everything else (logs, audit,
        ad-hoc ``json.dumps``) gets ``"***"``. ``None`` passes
        through unchanged so an absent key stays distinguishable from
        a redacted one.
        """
        if value is None:
            return None
        ctx = info.context or {}
        return value if ctx.get("include_secrets") else "***"

    # Pre-resolved fallback target — publisher calls
    # ``ResolvedConfig.resolve_fallback()`` once and ships the result so
    # the worker doesn't need ``ResolvedConfig`` machinery.
    fallback_provider: str | None = None
    fallback_model: str | None = None

    # Field names the *agent* explicitly supplied at write time (e.g.
    # ``["memory_type", "weight"]``). The worker MUST NOT overwrite
    # these on PATCH — agent-provided values always win, matching the
    # synchronous path's gating in ``memory_service.py``. ``None`` =
    # publisher hasn't classified, worker writes everything (used
    # before the core-api hot-path change in PR-C lands).
    agent_provided_fields: list[str] | None = None
