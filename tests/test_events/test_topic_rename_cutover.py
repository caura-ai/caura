"""Rename step 2: bind both names, publish exactly one.

A Pub/Sub topic cannot be renamed — it is created and deleted, and a
subscription cannot move between topics — so the cutover is expand, migrate,
contract. Step 2 is the migrate half's precondition: every subscriber holds the
old name *and* its twin, while publishing is untouched. That combination is what
makes flipping a publisher later a lossless operation instead of a gap.

Two properties carry the whole step, and both are asserted here rather than
described:

* **Publishing moves one family at a time.** ``lifecycle`` flipped on
  2026-08-28; every other family still targets the name it targeted before. The
  per-family assertions below are what stop a further family riding along in a
  diff that looks like it only touched one line — each flip has to restate the
  set, which is the point.
* **Never dual-publish.** One publish call produces exactly one message. The
  alternative cutover — publish to both names for a while — delivers every event
  twice to every subscriber bound to both, which is the failure the ordering
  exists to avoid.

The rest is about which way each default points, because the two backends need
opposite ones and a missing twin fails differently in each.

THE FLIP CHANGED WHAT A DEFAULT-CONSTRUCTED BUS MEANS HERE. With ``lifecycle``
in ``FLIPPED_FAMILIES`` and its twins unbound at ``dual=False``, the
construction guard refuses that combination — so ``PubSubEventBus(...)`` with
the flag off now raises in this repo too, by design. Tests that are about *the
flag's parse rule* rather than *the flip* therefore take the ``nothing_flipped``
fixture, which restores the pre-flip set for the duration. That is not the flip
being papered over: the parse rule genuinely does not depend on which families
are flipped, and the guard keeps its own dedicated tests further down.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.events import Event, InProcessEventBus, PubSubEventBus, Topics
from common.events import topics as topics_mod
from common.events.factory import get_event_bus, reset_event_bus_for_testing


@pytest.fixture
def nothing_flipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the pre-flip set, for tests about the flag rather than the flip.

    ``dual=False`` stopped being constructible here the moment ``lifecycle`` was
    flipped, because its twins are then published-to and unbound. Several tests
    below predate that and are asking a different question — does a blank value
    read as off, does the Pub/Sub backend bind one name by default — whose
    answer does not depend on the flipped set at all. Emptying the set is the
    narrowest way to keep asking it: the bus, the factory and the parse are all
    still the real ones, and only the guard's precondition is removed.

    The alternative, passing ``dual_subscribe=True``, would silently change what
    those tests assert — they would stop covering the default path, which is the
    one every standalone and on-prem deployment runs.
    """
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset())


@pytest.fixture
def bus() -> PubSubEventBus:
    # dual on: post-flip this is the only configuration a bus in this repo can
    # be constructed in, and it is what all 12 deployables actually run.
    b = PubSubEventBus(
        project_id="proj", subscription_prefix="test", dual_subscribe=True
    )
    fake_publisher = MagicMock(spec=["topic_path", "publish", "stop"])
    fake_publisher.topic_path = lambda proj, topic: f"projects/{proj}/topics/{topic}"
    future = MagicMock()
    future.result = MagicMock(return_value="msg-id-1")
    fake_publisher.publish = MagicMock(return_value=future)
    b._publisher = fake_publisher
    return b


async def handler(event: Event) -> None: ...


# ── the naming functions ─────────────────────────────────────────────


def test_renamed_rewrites_only_the_first_segment() -> None:
    assert topics_mod.renamed(Topics.Memory.EMBEDDED) == "caura.memory.embedded"
    assert (
        topics_mod.renamed(Topics.Lifecycle.CRYSTALLIZE_REQUESTED)
        == "caura.lifecycle.crystallize-requested"
    )


def test_renamed_is_idempotent() -> None:
    """Nothing can double-rename, so applying the expansion twice is harmless.

    This is why the derivation matches the first segment rather than the
    outgoing brand: it makes a half-migrated list safe to run through again.
    """
    once = topics_mod.renamed(Topics.Memory.EMBEDDED)
    assert topics_mod.renamed(once) == once


def test_renamed_leaves_a_nameless_topic_alone() -> None:
    assert topics_mod.renamed("no-dots-here") == "no-dots-here"


def test_family_is_the_middle_segment() -> None:
    # The unit the publisher flip is decided in, one family at a time.
    assert topics_mod.family(Topics.Audit.EVENT_RECORDED) == "audit"
    assert topics_mod.family(Topics.Pipeline.ENTITY_EXTRACTED) == "pipeline"
    assert topics_mod.family("no-dots-here") == ""


def test_subscribe_names_defaults_to_the_current_name_only() -> None:
    assert topics_mod.subscribe_names(Topics.Memory.EMBEDDED, dual=False) == (
        str(Topics.Memory.EMBEDDED),
    )


def test_subscribe_names_dual_returns_both_without_duplicates() -> None:
    both = topics_mod.subscribe_names(Topics.Memory.EMBEDDED, dual=True)
    assert both == (str(Topics.Memory.EMBEDDED), "caura.memory.embedded")
    # An already-renamed name must yield ONE entry, not the same string twice —
    # a duplicate would register the handler twice and double-dispatch it.
    assert topics_mod.subscribe_names("caura.memory.embedded", dual=True) == (
        "caura.memory.embedded",
    )


# ── publishing is unchanged: the property that makes step 2 shippable ─


# Hand-spelled rather than derived from ``topics_mod``. Deriving them would make
# every assertion below a tautology that holds for whatever the module happens to
# say, including a family nobody meant to add. Spelling them here means a flip
# has to be stated in exactly two places — the module, and this pair — and the
# equality in ``test_exactly_the_flipped_families_are_flipped`` is what turns the
# second one into a deliberate stop rather than a chore.
FLIPPED = frozenset({"lifecycle"})
# Per topic, not comprehended from ``FLIPPED`` over ``all_topics()``. A topic
# added to an already-flipped family is published under the new name from its
# first commit, so it needs its twin provisioned before it can ship; failing
# here is how whoever adds one finds that out.
FLIPPED_TOPICS = sorted(
    [
        str(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED),
        str(Topics.Lifecycle.ARCHIVE_STALE_REQUESTED),
        str(Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED),
        str(Topics.Lifecycle.CRYSTALLIZE_REQUESTED),
        str(Topics.Lifecycle.CRYSTALLIZE_ON_DEMAND_REQUESTED),
        str(Topics.Lifecycle.ENTITY_LINK_REQUESTED),
        str(Topics.Lifecycle.INSIGHTS_REQUESTED),
        str(Topics.Lifecycle.EMBED_BACKFILL_REQUESTED),
        str(Topics.Lifecycle.FORGE_DISTILL_REQUESTED),
    ]
)


def test_exactly_the_flipped_families_are_flipped() -> None:
    """The state of the flip, asserted at the one line that states it.

    Pinned as an EQUALITY rather than a membership check, so that adding a
    further family to that literal fails here. One family at a time is the whole
    discipline of this step: each flip is a separate change with its own
    readiness evidence, and a diff adding two families reads exactly like a diff
    adding one. This test failing is therefore the EXPECTED cost of a flip, and
    updating it is part of the change.

    ``lifecycle`` flipped 2026-08-28 — the first SHARED family, so the same
    change lands in caura-enterprise in the same cycle (rule 6). That repo's set
    also holds ``fleet`` and ``security``, which are declared only there; naming
    either here would raise at import in every OSS service.

    ``audit`` is called out because it must be flipped LAST — those rows are
    hash-chained, and a lost or reordered audit event is the one failure in this
    programme that cannot be undone.

    ``pipeline`` is called out for the opposite reason: it is declared in both
    repos but has no live topic in either environment, so flipping it would
    publish into nothing and raise nothing. A no-op flip that reports success is
    the worst outcome available in this cutover, because it also looks like
    progress.

    ``memory`` is the natural next family and is otherwise ready on the same
    evidence, but it declares ``.created``, which exists in neither environment
    — a smaller instance of what disqualifies ``pipeline``. Provision or remove
    that member before flipping it, so the flip carries no exception.
    """
    assert topics_mod.FLIPPED_FAMILIES == FLIPPED
    assert "audit" not in topics_mod.FLIPPED_FAMILIES
    assert "pipeline" not in topics_mod.FLIPPED_FAMILIES
    assert "memory" not in topics_mod.FLIPPED_FAMILIES


def test_known_families_are_derived_from_the_enums() -> None:
    """The set a flip is validated against comes from the topics themselves."""
    assert topics_mod.known_families() == {
        "memory",
        "audit",
        "pipeline",
        "lifecycle",
        "org",
    }


def test_a_misspelled_flipped_family_is_refused() -> None:
    """A typo must not be a silent no-op.

    ``publish_name`` looks the family up by string, so ``"audi"`` would simply
    never match: every topic keeps its outgoing name, the flip reports success,
    no traffic moves, and the twin subscriptions sit idle with nothing anywhere
    saying why.

    The guard runs at import, so this re-executes the real module source with
    the literal edited — the same edit a fat-fingered flip would make — rather
    than re-implementing the check and asserting the copy raises.
    """
    original = Path(topics_mod.__file__).read_text(encoding="utf-8")
    # Match the VALUE EXPRESSION — ``frozenset(...)`` through its closing paren —
    # rather than the literal set or the physical line. The previous form named
    # ``frozenset()`` outright and stopped matching the moment a family was
    # added, which is the one change that must not silently disarm this test.
    #
    # A line-anchored ``^...$`` is the tempting replacement and is also wrong,
    # which is worth recording because it fails SILENTLY. ``common/ruff.toml``
    # sets line-length 88; this assignment is 59 characters at one family and
    # passes 88 at four, so ``ruff format`` will eventually wrap it across three
    # lines. A line anchor then rewrites only the first physical line, leaving
    # the old set body orphaned and indented — the count check below still sees
    # exactly one match, and the test fails with ``IndentationError`` instead of
    # the ``ValueError`` it is asserting, pointing nowhere near the cause.
    # Matching to the closing paren handles the flat and wrapped forms
    # identically, and the optional ``{...}`` also covers the bare
    # ``frozenset()`` this returns to after the contract step.
    #
    # The count assertion is what keeps the wildcard honest: a zero-match would
    # otherwise exec an unmodified module, raise nothing, and pass vacuously.
    prefix = "FLIPPED_FAMILIES: frozenset[str] = "
    pattern = re.compile(re.escape(prefix) + r"frozenset\(\s*(?:\{[^}]*\})?\s*\)")
    source, count = pattern.subn(f'{prefix}frozenset({{"audi"}})', original)
    assert count == 1, (
        f"expected exactly one {prefix!r} assignment to edit, found {count}; "
        "the literal moved and this test is no longer editing it"
    )
    assert source != original

    with pytest.raises(ValueError, match="match no topic family"):
        exec(  # noqa: S102 — executing our own module source, with one literal edited
            compile(source, topics_mod.__file__, "exec"),
            {"__name__": "_topics_under_test"},
        )


def test_the_real_module_passes_its_own_guard() -> None:
    """Counterpart, so the test above cannot pass because the guard is unreachable."""
    assert topics_mod.FLIPPED_FAMILIES - topics_mod.known_families() == frozenset()


def test_publish_name_is_the_identity_for_every_unflipped_family() -> None:
    """Every unflipped family still publishes under the name it always did.

    Enumerated from ``all_topics()`` rather than a hand-listed sample, so a
    family flipped without updating this file fails here instead of passing
    because nobody thought to add its topic to the list.
    """
    for topic in topics_mod.all_topics():
        if topics_mod.family(topic) in FLIPPED:
            continue
        assert topics_mod.publish_name(topic) == str(topic)


def test_publish_name_moves_the_flipped_family_and_only_it() -> None:
    """The flip itself: ``lifecycle`` publishes under the twin, nothing else."""
    assert topics_mod.publish_name(Topics.Lifecycle.INSIGHTS_REQUESTED) == (
        "caura.lifecycle.insights-requested"
    )
    # The twin literals themselves are pinned by
    # ``test_renamed_rewrites_only_the_first_segment``; re-asserting them here
    # would just duplicate it. What this test adds is that ``publish_name``
    # actually routes to them, and that NOTHING ELSE moves.
    moved = sorted(
        t for t in topics_mod.all_topics() if topics_mod.publish_name(t) != str(t)
    )
    assert moved == FLIPPED_TOPICS, (
        f"only the flipped family may change publish name; these moved: {moved}"
    )


async def test_publish_targets_the_unrenamed_topic(bus: PubSubEventBus) -> None:
    await bus.publish(
        Topics.Memory.EMBED_REQUESTED, Event(event_type=Topics.Memory.EMBED_REQUESTED)
    )
    topic_path, _ = bus._publisher.publish.call_args[0]
    assert topic_path == f"projects/proj/topics/{Topics.Memory.EMBED_REQUESTED}"


async def test_dual_subscribe_does_not_dual_publish(bus: PubSubEventBus) -> None:
    """One publish, one message — even with both names bound.

    Publishing to both names is the version of this cutover that duplicates
    every event for every subscriber holding both, which after this step is all
    of them.
    """
    bus._dual_subscribe = True
    bus.subscribe(Topics.Memory.EMBEDDED, handler)
    await bus.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))

    assert bus._publisher.publish.call_count == 1
    topic_path, data = bus._publisher.publish.call_args[0]
    assert topic_path == f"projects/proj/topics/{Topics.Memory.EMBEDDED}"
    # The envelope is untouched too: event_type is payload, not routing.
    assert json.loads(data.decode())["event_type"] == str(Topics.Memory.EMBEDDED)


# ── the Pub/Sub backend: off by default, because on is not survivable ─


def test_pubsub_subscribe_binds_one_name_by_default(nothing_flipped: None) -> None:
    """Default OFF, and the registry is byte-identical to before the change.

    On this backend a topic name is a provisioned subscription. Binding one that
    does not exist yet is a permanent NotFound that halts the pull loop and
    turns the readiness probe red, so the default has to be the one that ships
    safely into an environment whose infrastructure has not been applied.
    """
    b = PubSubEventBus(project_id="proj", subscription_prefix="core-api")
    b.subscribe(Topics.Memory.EMBEDDED, handler)
    assert list(b._handlers) == [str(Topics.Memory.EMBEDDED)]


def test_pubsub_subscribe_binds_both_names_when_enabled() -> None:
    b = PubSubEventBus(
        project_id="proj", subscription_prefix="core-api", dual_subscribe=True
    )
    b.subscribe(Topics.Memory.EMBEDDED, handler)
    assert sorted(b._handlers) == [
        "caura.memory.embedded",
        str(Topics.Memory.EMBEDDED),
    ]
    # One handler per name, not two on one name.
    assert all(len(hs) == 1 for hs in b._handlers.values())


def test_broadcast_flag_carries_to_the_twin() -> None:
    """The trap inside this step, and the reason the expansion lives in the bus.

    A broadcast topic deliberately has NO durable subscription — each process
    creates an ephemeral one at runtime. If the twin is bound but left out of
    the broadcast set, start() treats it as an ordinary work queue and opens a
    pull loop against a ``<prefix>--<twin>`` subscription that was never
    provisioned and never will be. That is a permanent NotFound and a red
    health endpoint, on the one topic whose entire design is that it degrades
    quietly.
    """
    b = PubSubEventBus(
        project_id="proj", subscription_prefix="core-api", dual_subscribe=True
    )
    b.subscribe(Topics.Org.SETTINGS_CHANGED, handler, broadcast=True)
    b.subscribe(Topics.Memory.EMBEDDED, handler)

    assert b._broadcast_topics == {
        str(Topics.Org.SETTINGS_CHANGED),
        "caura.org.settings-changed",
    }
    # And the work-queue topic's twin is NOT broadcast — it has a real durable
    # subscription and must keep using it.
    assert "caura.memory.embedded" not in b._broadcast_topics


# ── the in-process backend: on always, because off is not survivable ──


def test_inprocess_binds_both_names_with_no_flag() -> None:
    """Opposite default, for the same reason the Pub/Sub one is OFF.

    Here a name is a dict key: binding one costs nothing and cannot fail. What
    it buys is that standalone and on-prem deployments — which never run the
    Terraform the Pub/Sub flag is gated on — keep dispatching after a family is
    flipped, instead of silently delivering to nobody.
    """
    b = InProcessEventBus()
    b.subscribe(Topics.Memory.EMBEDDED, handler)
    assert sorted(b._handlers) == [
        "caura.memory.embedded",
        str(Topics.Memory.EMBEDDED),
    ]


async def test_inprocess_dispatches_exactly_once_despite_both_bindings() -> None:
    b = InProcessEventBus()
    seen: list[str] = []

    async def record(event: Event) -> None:
        seen.append(str(event.event_id))

    b.subscribe(Topics.Memory.EMBEDDED, record)
    await b.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))
    await b.drain()
    assert len(seen) == 1


async def test_a_flipped_family_still_reaches_a_dual_bound_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payoff: the flip is lossless *because* the subscriber holds both.

    Simulates step 4 for one family. The publisher moves to the renamed topic
    and the handler — subscribed under the old name, bound to both — still
    receives it, exactly once. Without the dual binding this same flip delivers
    the event to nobody, with no error on either side.
    """
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    b = InProcessEventBus()
    seen: list[str] = []

    async def record(event: Event) -> None:
        seen.append(str(event.event_type))

    b.subscribe(Topics.Memory.EMBEDDED, record)
    await b.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))
    await b.drain()
    assert len(seen) == 1

    # A family that has NOT flipped is unaffected by the one that has.
    assert topics_mod.publish_name(Topics.Audit.EVENT_RECORDED) == str(
        Topics.Audit.EVENT_RECORDED
    )


async def test_a_flipped_family_reaches_nobody_without_the_dual_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterfactual, so the test above cannot pass for the wrong reason.

    Bind the old name only — the pre-step-2 world — then flip the publisher.
    The event goes nowhere and nothing raises, which is precisely why this
    ordering is not optional.
    """
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    b = InProcessEventBus()
    seen: list[str] = []

    async def record(event: Event) -> None:
        seen.append(str(event.event_type))

    b._handlers[str(Topics.Memory.EMBEDDED)].append(record)  # single-name binding
    await b.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))
    await b.drain()
    assert seen == []


# ── the flag: blank must mean off ────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("tru", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("on", True),
    ],
)
async def test_dual_subscribe_flag_reads_anything_but_an_explicit_yes_as_off(
    monkeypatch: pytest.MonkeyPatch,
    nothing_flipped: None,
    raw: str | None,
    expected: bool,
) -> None:
    """Which way the default points, asked of the flag itself.

    A blank value is the realistic failure: a deploy template that has started
    listing the new variable with nothing filled in yet. Read as "yes" it turns
    on dual-subscribe in an environment with no twin subscriptions and 503s every
    consumer; read as "no" it means the cutover has not started, which the next
    step verifies per service regardless. Only the second is recoverable by
    doing nothing.
    """
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    if raw is None:
        monkeypatch.delenv("EVENT_BUS_DUAL_SUBSCRIBE", raising=False)
    else:
        monkeypatch.setenv("EVENT_BUS_DUAL_SUBSCRIBE", raw)

    # The factory fails fast at boot when the Pub/Sub SDK is absent; it is not
    # installed in the OSS test env and is irrelevant to reading a flag.
    monkeypatch.setattr(PubSubEventBus, "_ensure_pubsub_sdk", staticmethod(lambda: None))

    try:
        bus = get_event_bus()
        assert isinstance(bus, PubSubEventBus)
        assert bus._dual_subscribe is expected
    finally:
        await reset_event_bus_for_testing()


# ── the guard: refuse to publish where nothing is bound (#913) ───────────────
#
# The combination the rest of this module documents as the one that fails with
# no signal at all — a flipped family publishing under its renamed name while
# the subscriber binds only the current one — is now refused at construction
# instead of merely being described. These tests pin the refusal AND its
# boundaries, because a guard that over-fires here would take out the default
# configuration rather than the broken one.


def test_nothing_publishes_unbound_in_the_configuration_the_services_run() -> None:
    """The shipped-state property, restated for a world with a family flipped.

    Before the flip this asserted emptiness at BOTH settings, which was the
    byte-identical-behaviour claim that made binding both names safe to ship
    ahead of any deploy. That claim is spent: flipping a publisher is precisely
    the point at which behaviour stops being identical, and ``dual=False``
    becomes a state this repo must refuse rather than one it must support.

    What has to hold now is narrower and is the actual precondition of the flip:
    at ``dual=True`` — the setting every one of the 12 pubsub-backed deployables
    was confirmed running at its live revision before this landed — no topic
    publishes anywhere unbound. That is the property that makes the flip
    lossless.
    """
    assert topics_mod.unbound_publish_topics(dual=True) == ()


def test_the_flip_is_hazardous_at_the_default_and_says_so() -> None:
    """The counterfactual, so the test above cannot pass by the guard being dead.

    ``dual=False`` is the default and is what standalone and on-prem
    deployments run. Post-flip it is genuinely unsafe HERE, and the module has
    to name every offending topic rather than merely return a bool — the
    operator reading this is deciding whether their environment is the one that
    is wrong, and there are nine names to give them.
    """
    unbound = topics_mod.unbound_publish_topics(dual=False)
    assert sorted(unbound) == FLIPPED_TOPICS


def test_unbound_publish_topics_names_a_flipped_family_when_dual_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipped + dual off is the hazard, and it is reported per topic."""
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    unbound = topics_mod.unbound_publish_topics(dual=False)
    assert unbound, "a flipped family with dual off must be reported"
    assert all(topics_mod.family(t) == "memory" for t in unbound)
    # Every reported topic really would go nowhere: the name it publishes under
    # is absent from the names a dual=False subscriber binds.
    for topic in unbound:
        assert topics_mod.publish_name(topic) not in topics_mod.subscribe_names(
            topic, dual=False
        )
    # A family that has NOT flipped is not swept up in the report.
    assert not any(topics_mod.family(t) == "audit" for t in unbound)


def test_dual_subscribe_makes_a_flipped_family_bound_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same flip with dual on is exactly what step 2 bought."""
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    assert topics_mod.unbound_publish_topics(dual=True) == ()


def test_pubsub_bus_refuses_to_construct_when_a_flip_has_nothing_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard itself: the bus will not exist in the silent-failure state.

    At construction rather than ``start()`` — a publish-only process never calls
    ``start()``, so a check there would leave the write side unguarded, which is
    the side that does the losing.
    """
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    with pytest.raises(ValueError, match="does not bind"):
        PubSubEventBus(project_id="proj", subscription_prefix="test")


def test_pubsub_bus_constructs_when_the_flip_is_matched_by_dual_subscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is about the MISMATCH, not about having flipped anything."""
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))
    b = PubSubEventBus(
        project_id="proj", subscription_prefix="test", dual_subscribe=True
    )
    assert b._dual_subscribe is True


def test_the_guard_does_not_fire_once_a_family_is_fully_contracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false positive an emptiness check would cause — the reason this guard
    compares names instead of testing ``FLIPPED_FAMILIES`` for emptiness.

    After the contract step a family's enum members ARE the renamed names.
    ``renamed`` is idempotent, so ``publish_name`` and
    ``subscribe_names(dual=False)`` agree and there is no twin left to bind:
    ``dual=False`` is not merely tolerable there, it is correct. A guard keyed on
    "is FLIPPED_FAMILIES non-empty" would refuse to start those processes — and
    ``dual=False`` is the DEFAULT, so it would take out precisely the standalone
    and on-prem deployments that never run the Terraform the flag is gated on.

    Simulated by declaring a topic whose name already carries the new prefix,
    which is what the contract step leaves behind.
    """
    contracted = topics_mod.RENAMED_PREFIX + "memory.embedded"
    assert topics_mod.renamed(contracted) == contracted, "precondition: idempotent"
    monkeypatch.setattr(topics_mod, "all_topics", lambda: (contracted,))
    monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", frozenset({"memory"}))

    # Non-empty FLIPPED_FAMILIES, dual off — and yet nothing is unbound.
    assert topics_mod.FLIPPED_FAMILIES
    assert topics_mod.unbound_publish_topics(dual=False) == ()
    PubSubEventBus(project_id="proj", subscription_prefix="test")


def test_inprocess_bus_can_never_reach_the_guarded_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the in-process backend needs no guard: it binds both, always.

    ``InProcessEventBus.subscribe`` calls ``subscribe_names(..., dual=True)``
    unconditionally, so the mismatch this guard catches is unreachable there for
    any value of ``FLIPPED_FAMILIES``.
    """
    for flipped in (frozenset(), frozenset({"memory"}), topics_mod.known_families()):
        monkeypatch.setattr(topics_mod, "FLIPPED_FAMILIES", flipped)
        assert topics_mod.unbound_publish_topics(dual=True) == ()
