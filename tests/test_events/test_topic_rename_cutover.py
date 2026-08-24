"""Rename step 2: bind both names, publish exactly one.

A Pub/Sub topic cannot be renamed — it is created and deleted, and a
subscription cannot move between topics — so the cutover is expand, migrate,
contract. Step 2 is the migrate half's precondition: every subscriber holds the
old name *and* its twin, while publishing is untouched. That combination is what
makes flipping a publisher later a lossless operation instead of a gap.

Two properties carry the whole step, and both are asserted here rather than
described:

* **Publishing is unchanged.** No family is flipped, so every publish still
  targets the name it targeted before. If this stops being true the step is no
  longer a runtime no-op and can no longer be shipped ahead of a deploy.
* **Never dual-publish.** One publish call produces exactly one message. The
  alternative cutover — publish to both names for a while — delivers every event
  twice to every subscriber bound to both, which is the failure the ordering
  exists to avoid.

The rest is about which way each default points, because the two backends need
opposite ones and a missing twin fails differently in each.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.events import Event, InProcessEventBus, PubSubEventBus, Topics
from common.events import topics as topics_mod
from common.events.factory import get_event_bus, reset_event_bus_for_testing


@pytest.fixture
def bus() -> PubSubEventBus:
    b = PubSubEventBus(project_id="proj", subscription_prefix="test")
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


def test_no_family_is_flipped_yet() -> None:
    """The literal precondition of this step.

    Asserted directly rather than inferred from the publish tests below, so that
    flipping a family in a later step fails HERE, at the one line that says the
    cutover has not started, instead of somewhere that reads like a broken test.
    """
    assert topics_mod.FLIPPED_FAMILIES == frozenset()


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
    target = "FLIPPED_FAMILIES: frozenset[str] = frozenset()"
    assert target in original, "the literal moved; this test is no longer editing it"
    source = original.replace(target, f'{target[:-11]}frozenset({{"audi"}})')
    assert source != original

    with pytest.raises(ValueError, match="match no topic family"):
        exec(  # noqa: S102 — executing our own module source, with one literal edited
            compile(source, topics_mod.__file__, "exec"),
            {"__name__": "_topics_under_test"},
        )


def test_the_real_module_passes_its_own_guard() -> None:
    """Counterpart, so the test above cannot pass because the guard is unreachable."""
    assert topics_mod.FLIPPED_FAMILIES - topics_mod.known_families() == frozenset()


def test_publish_name_is_the_identity_while_nothing_is_flipped() -> None:
    for topic in (
        Topics.Memory.EMBEDDED,
        Topics.Audit.EVENT_RECORDED,
        Topics.Org.SETTINGS_CHANGED,
        Topics.Lifecycle.INSIGHTS_REQUESTED,
    ):
        assert topics_mod.publish_name(topic) == str(topic)


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


def test_pubsub_subscribe_binds_one_name_by_default() -> None:
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
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
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


def test_unbound_publish_topics_is_empty_in_the_shipped_state() -> None:
    """Nothing flipped: no topic publishes anywhere unbound, either way.

    The byte-identical-behaviour property. If this fails, merging this guard
    stopped being a runtime no-op.
    """
    assert topics_mod.FLIPPED_FAMILIES == frozenset()
    assert topics_mod.unbound_publish_topics(dual=False) == ()
    assert topics_mod.unbound_publish_topics(dual=True) == ()


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
