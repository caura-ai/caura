"""Canonical topic names as str-valued enum members.

Convention: `memclaw.<domain>.<verb-past-participle>` for events that
announce something that already happened, `.<verb-requested>` for events
that ask a subscriber to do work.

Uses `enum.StrEnum` (Python 3.11+) so members behave like the underlying
string in every context: equality, dict-key hashing, f-string formatting,
and Pub/Sub `topic_path` building all see `Topics.Memory.EMBEDDED` as
its literal string value. A plain `(str, enum.Enum)` mix
equates but does NOT format as the value — `f"{M.X}"` returns
`"M.X"` — which would corrupt any string-formatted use site.
"""

from __future__ import annotations

import enum


class Memory(enum.StrEnum):
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
    `Topics.Memory.EMBEDDED` instead of importing each inner enum."""

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
# Add ONE family at a time, and only once every subscriber of that family is
# confirmed deployed and bound to both names — confirmed per running service,
# not per merged pull request; a merge is not a vendor and a vendor is not a
# deploy.
#
# THIS SET IS NOT THE SAME IN BOTH REPOS, and mirroring the enterprise line
# here would break this one. ``fleet`` and ``security`` are declared only in
# enterprise, so naming either here makes ``_validate_flipped_families`` raise
# at import in every OSS service. Rule 6 couples the two repos for a SHARED
# family — which ``lifecycle`` is — and inverts for an enterprise-only one.
#
# ``lifecycle`` flipped 2026-08-28 — the third family in this programme and the
# first SHARED one, so it lands in both repos in one cycle. Chosen over
# ``memory``, the other remaining shared family, on provisioning completeness
# rather than size: every one of the 9 topics this family declares is live in
# both environments, whereas ``memory`` then declared one topic (``.created``)
# that existed in neither. That is the same defect that disqualifies
# ``pipeline``, and a flip is not the step at which to rely on a topic being
# harmless because nothing publishes it. That declaration has since been
# removed — see the ``memory`` note below. Evidence, measured against the
# running world rather than the source tree:
#
#   * 12/12 pubsub-backed deployables reported EVENT_BUS_DUAL_SUBSCRIBE on at
#     their RUNNING revision (``check_flip_readiness.py``, enterprise).
#   * 36/36 legacy durable subscriptions across prod and staging (18 each) had
#     a twin, each verified attached to the matching twin TOPIC and not merely
#     present by name.
#   * ``preflight_dual_subscribe.py`` exited 0 for both environments, with the
#     topic-IAM half reachable, so the attach pairing was verified live and not
#     merely in config.
#   * This family has NO broadcast topic and no per-process ephemeral
#     subscription in either environment, so there is no runtime-created
#     binding to infer — every subscription it relies on is a durable resource
#     that was listed.
#
# WHAT THAT DOES NOT PROVE. Both gates read CONFIGURATION. Neither observes a
# message being delivered. A green gate is a necessary condition, never
# evidence the flip works; the end-to-end signal is the staging deploy's
# control-plane check. Do not report a green gate as a working flip.
#
# ``memory`` is next and is ready on the same evidence — 16/16 twin durable
# subscriptions, no ephemerals. Its one blocker is cleared: the declared
# ``.created`` topic was removed rather than provisioned, because it existed in
# neither environment, appeared in no events manifest, and had no publisher or
# subscriber in either repo. Provisioning it would have created topics that
# nothing emits to and nothing reads.
#
# ``pipeline`` must NOT enter this set while it has zero live topics in either
# environment: publishing to a topic that does not exist is silent loss, so
# flipping it would move nothing and report success. ``org`` is shared AND its
# staging ephemeral pool was under investigation as a suspected subscription
# leak. ``audit`` LAST, unconditionally — those rows are hash-chained, and a
# lost or reordered audit event is the one failure here that replay cannot
# repair.
FLIPPED_FAMILIES: frozenset[str] = frozenset({"lifecycle"})


def all_topics() -> tuple[str, ...]:
    """Every topic declared in this module.

    Derived by walking the enums rather than listed, so it cannot drift from
    them, and so the same code yields the right answer in each repo despite the
    two copies declaring different topics.
    """
    return tuple(
        str(member)
        for attr in vars(Topics).values()
        if isinstance(attr, type) and issubclass(attr, enum.StrEnum)
        for member in attr
    )


def known_families() -> frozenset[str]:
    """Every family the topics declared in this module actually use."""
    return frozenset(f for topic in all_topics() if (f := family(topic)))


# A family named here that does not exist would be a SILENT no-op: publish_name
# would go on returning the outgoing name for every topic, so the flip would
# look done, move nothing, and leave the twin subscriptions idle with no error
# anywhere to say so. That is the same shape as every other failure in this
# cutover — quiet, and only visible if you already suspected it — so it is
# checked at the point of definition rather than left to a reviewer's eye.
#
# Raising here is a deliberate choice about blast radius. This set is a literal
# in this file, so the only way to trip it is editing that literal, and the
# import runs in every test that touches the bus — a typo cannot get past CI,
# let alone reach a deploy. The cost of being wrong is an ImportError in front
# of the person who made the typo; the cost of NOT checking is a step 4 that
# reports success and moves no traffic.
def _validate_flipped_families() -> None:
    """Raise if FLIPPED_FAMILIES names a family that does not exist.

    A function rather than a bare module-level check so nothing is left behind in
    the module namespace once it has run, and so ``known_families()`` is
    evaluated once for both the comparison and the message.
    """
    known = known_families()
    if unknown := FLIPPED_FAMILIES - known:
        raise ValueError(
            f"FLIPPED_FAMILIES names {sorted(unknown)}, which match no topic "
            f"family declared here (known: {sorted(known)}). A family that does "
            "not exist flips nothing and reports no error — fix the spelling."
        )


_validate_flipped_families()


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


def unbound_publish_topics(*, dual: bool) -> tuple[str, ...]:
    """Topics this module would publish under a name no subscriber of them binds.

    The one combination in this cutover that fails with no signal at all. A
    flipped family publishes under its renamed name; a subscriber running with
    ``dual=False`` binds only the current one. The message lands on a topic
    nothing is pulling — no exception, no ``NotFound``, a green readiness probe,
    and zero delivered. Every other ordering mistake here is loud: binding a twin
    that was never provisioned is a permanent ``NotFound`` that reds the health
    endpoint, and naming a family that does not exist raises at import.

    Asking the two functions directly, rather than testing ``FLIPPED_FAMILIES``
    for emptiness, is what makes this exact instead of approximate. Non-empty
    ``FLIPPED_FAMILIES`` is only a PROXY for the hazard, and it over-fires in a
    state this cutover really passes through: once a family's enum members have
    themselves been renamed (the contract step), ``renamed`` is the identity for
    them, so ``publish_name`` and ``subscribe_names(dual=False)`` agree and
    ``dual=False`` is not merely safe but correct — there is no twin left to
    bind. A guard keyed on emptiness would refuse to start those processes, and
    ``dual=False`` is the DEFAULT, so it would take out precisely the standalone
    and on-prem deployments that never run the Terraform the flag is gated on.
    Comparing the names cannot make that mistake: agreement is agreement however
    it arose.

    Returns the offending topics rather than a bool so a caller can name them.
    """
    return tuple(
        topic
        for topic in all_topics()
        if publish_name(topic) not in subscribe_names(topic, dual=dual)
    )
