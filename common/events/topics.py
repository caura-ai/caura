"""Canonical topic names as str-valued enum members.

Convention: `memclaw.<domain>.<verb-past-participle>` for events that
announce something that already happened, `.<verb-requested>` for events
that ask a subscriber to do work.

Uses `enum.StrEnum` (Python 3.11+) so members behave like the underlying
string in every context: equality, dict-key hashing, f-string formatting,
and Pub/Sub `topic_path` building all see `Topics.Memory.CREATED` as
the literal `"memclaw.memory.created"`. A plain `(str, enum.Enum)` mix
equates but does NOT format as the value — `f"{M.X}"` returns
`"M.X"` — which would corrupt any string-formatted use site.
"""

from __future__ import annotations

import enum


class Memory(enum.StrEnum):
    CREATED = "memclaw.memory.created"
    EMBED_REQUESTED = "memclaw.memory.embed-requested"
    EMBEDDED = "memclaw.memory.embedded"
    ENRICH_REQUESTED = "memclaw.memory.enrich-requested"
    ENRICHED = "memclaw.memory.enriched"


class Audit(enum.StrEnum):
    EVENT_RECORDED = "memclaw.audit.event-recorded"


class Pipeline(enum.StrEnum):
    ENTITY_EXTRACT_REQUESTED = "memclaw.pipeline.entity-extract-requested"
    ENTITY_EXTRACTED = "memclaw.pipeline.entity-extracted"


class Org(enum.StrEnum):
    # CAURA-694: enterprise platform-admin-api publishes one event per
    # soft-delete + restore, the payload carries the affected tenant_ids
    # and an ``action: suppress | restore`` discriminator. Core-worker
    # subscribes and mirrors the decision into ``public.tenant_suppression``
    # so the OSS boundary guard (core-api) can reject reads/writes for
    # affected tenants synchronously, even while the durable mirror
    # eventually catches up.
    SUPPRESSION_CHANGED = "memclaw.org.suppression-changed"
    # CAURA-571: core-api publishes this after an org's settings are written so
    # every process drops its per-process settings cache promptly — without it,
    # a tightened governance control keeps applying its looser prior value on
    # sibling workers for up to the cache TTL (5 min). Subscribe with
    # ``broadcast=True`` (every process must receive it), not the work-queue
    # default.
    SETTINGS_CHANGED = "memclaw.org.settings-changed"


class Lifecycle(enum.StrEnum):
    # One topic per action — matches the `memclaw.memory.embed-requested`
    # vs `memclaw.memory.enrich-requested` convention. Keeping each
    # operation on its own topic gives clean per-subscription filtering
    # and lets each action evolve its payload independently.
    ARCHIVE_EXPIRED_REQUESTED = "memclaw.lifecycle.archive-expired-requested"
    ARCHIVE_STALE_REQUESTED = "memclaw.lifecycle.archive-stale-requested"
    PURGE_SOFT_DELETED_REQUESTED = "memclaw.lifecycle.purge-soft-deleted-requested"
    # CAURA-657: pipeline ops. Subscriber is core-api (NOT core-worker)
    # because the consumer needs core-api's pipeline machinery —
    # ``run_crystallization`` and ``build_full_entity_linking_pipeline``
    # both live there and have transitive deps the worker doesn't carry.
    CRYSTALLIZE_REQUESTED = "memclaw.lifecycle.crystallize-requested"
    # OSS #817: the SAME operation, triggered by ``POST /crystallize`` instead of
    # the nightly fanout, and on its own topic because the fanout's handler is a
    # poor fit for an on-demand request — it needs a ``lifecycle_audit`` row to
    # report into, and it dedups on a 24h window, which would silently skip a
    # person asking for a run because last night's succeeded. Consumer is core-api
    # for the same reason as above. One message per request; the run is not bounded
    # by an HTTP request budget, which is the whole point — completing a real run
    # does not fit in one. See ``common.events.crystallize_on_demand_request``.
    CRYSTALLIZE_ON_DEMAND_REQUESTED = (
        "memclaw.lifecycle.crystallize-on-demand-requested"
    )
    ENTITY_LINK_REQUESTED = "memclaw.lifecycle.entity-link-requested"
    INSIGHTS_REQUESTED = "memclaw.lifecycle.insights-requested"
    # Periodic sweep that re-embeds rows whose embedding is still NULL.
    # Subscriber is core-worker, which owns ``core_worker.backfill`` — the
    # only place that pages ``/memories/null-embedding-ids`` and republishes
    # EMBED_REQUESTED per row. One message per org; the per-org page loop
    # runs in the consumer, so it is not bounded by an HTTP request budget.
    EMBED_BACKFILL_REQUESTED = "memclaw.lifecycle.embed-backfill-requested"
    # Skill Factory SF-007: Forge resident publishes one of these per
    # scheduled distillation run. Stub handler in Phase 0 (just logs);
    # real handler arrives in Phase 1 with the cluster fingerprint and
    # distillation pipeline. See ``common.events.lifecycle_forge_request``.
    FORGE_DISTILL_REQUESTED = "memclaw.lifecycle.forge-distill-requested"


class Topics:
    """Namespaced facade so call sites keep the ergonomic form
    `Topics.Memory.CREATED` instead of importing each inner enum."""

    Memory = Memory
    Audit = Audit
    Pipeline = Pipeline
    Lifecycle = Lifecycle
    Org = Org
