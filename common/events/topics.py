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
    ENTITY_LINK_REQUESTED = "memclaw.lifecycle.entity-link-requested"
    INSIGHTS_REQUESTED = "memclaw.lifecycle.insights-requested"


class Topics:
    """Namespaced facade so call sites keep the ergonomic form
    `Topics.Memory.CREATED` instead of importing each inner enum."""

    Memory = Memory
    Audit = Audit
    Pipeline = Pipeline
    Lifecycle = Lifecycle
