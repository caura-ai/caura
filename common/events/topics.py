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


# ── Brand rename: one publish name, a set of subscribe names ────────────────
#
# A Pub/Sub topic cannot be renamed. It is created and deleted, and a
# subscription cannot move between topics, so the cutover is expand -> migrate
# -> contract: create twin topics, have every subscriber bind both names, flip
# the publishers one family at a time, drain the old subscriptions, then delete
# them.
#
# What that needs from this module is a distinction it did not previously have
# to make, because one name served both roles:
#
#   * the PUBLISH name — exactly one, always. Publishing an event under both
#     names at once delivers it twice to every subscriber bound to both, which
#     is the precise failure the ordering above exists to avoid. There is
#     deliberately no function here that returns more than one publish name, so
#     "just publish to both for a while" is not something this API can express.
#   * the SUBSCRIBE set — one name or two. A subscriber holding both is what
#     makes flipping a publisher lossless: the message lands on whichever name
#     the publisher used, and a handler is already waiting on it.
#
# The enum members above are deliberately untouched. They still ARE their
# string values, so equality, dict-key hashing, f-string formatting and
# ``topic_path`` building all keep working exactly as the module docstring
# promises. Everything below is derived from them.

RENAMED_PREFIX = "caura."


def renamed(topic: str) -> str:
    """The post-rename name for ``topic``.

    Rewrites the FIRST dot-segment rather than matching the outgoing brand by
    name, mirroring the ``replace(n, "/^[^.]+\\./", ...)`` that derives the twin
    topics in Terraform. Two consequences worth having: it is idempotent, so a
    name already carrying the new prefix maps to itself and nothing can
    double-rename; and this file gains no new occurrence of the outgoing brand
    for the rule-7 ratchet to count. A name with no dot is returned unchanged.
    """
    _, dot, rest = str(topic).partition(".")
    return RENAMED_PREFIX + rest if dot else str(topic)


def family(topic: str) -> str:
    """The topic family — the segment between the brand and the event name.

    ``<brand>.pipeline.entity-extracted`` -> ``pipeline``. Publishers flip one
    family at a time, so this is the unit that decision is made in. Returns ""
    for a name that has no family segment.
    """
    parts = str(topic).split(".")
    return parts[1] if len(parts) > 2 else ""


# Families whose PUBLISHERS have been flipped to the renamed topics.
#
# Empty, and that is the point: with nothing here, ``publish_name`` is the
# identity and publishing is byte-for-byte what it was before this module
# changed. That is what makes binding both names safe to ship ahead of any
# particular environment's deploy.
#
# Flipping a family is a separate, later step. Add ONE family at a time, and
# only once every subscriber of that family is confirmed deployed and bound to
# both names — confirmed per running service, not per merged pull request; a
# merge is not a vendor and a vendor is not a deploy. Order by blast radius:
# "pipeline" or "org" first, and "audit" LAST, because those rows are
# hash-chained and a lost or reordered audit event is the one failure in this
# programme that cannot be undone.
FLIPPED_FAMILIES: frozenset[str] = frozenset()


def publish_name(topic: str) -> str:
    """The single name to publish ``topic`` under.

    Returns one name. Never two — see the note above on why dual-publishing is
    the version of this cutover that duplicates every event.
    """
    return renamed(topic) if family(topic) in FLIPPED_FAMILIES else str(topic)


def subscribe_names(topic: str, *, dual: bool) -> tuple[str, ...]:
    """Every name a subscriber of ``topic`` has to bind.

    ``dual=False`` returns just the current name, which is the default and
    keeps a process's subscription set identical to what it was. ``dual=True``
    returns both, and is only safe where the twin subscription actually exists —
    on the Pub/Sub backend a subscription that is absent is a permanent
    ``NotFound``, which halts the pull loop and takes the health endpoint down.
    Never returns a duplicate, so a name that is already renamed yields one
    entry rather than the same string twice.
    """
    current = str(topic)
    if not dual:
        return (current,)
    new = renamed(current)
    return (current,) if new == current else (current, new)
