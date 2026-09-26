"""What the event bus promises, and what it actually does.

Three gaps, all of the same kind: the bus was described somewhere other than
where it behaves, and the two disagreed.

* ``base.py`` said in-process buses "re-raise in tests". No such mechanism has
  ever existed — ``_safe_invoke`` swallows unconditionally.
* ``inprocess.py`` called that swallow a match for "Pub/Sub-style
  fire-and-forget semantics". Pub/Sub is not fire-and-forget: it redelivers
  exactly the messages this bus discards.
* ``MemoryEmbedRequest`` was the only schema in ``common/events`` with
  ``extra="forbid"``, against a policy the other five state in prose.

The manifest gap is the fourth and the only structural one: a shared registrar
in a module the generator did not know about was invisible to the generator AND
to the guard test, so a consumed topic could ship with no Terraform
subscription.

The two corrected docstrings carry no test of their own, deliberately. A
phrase-absence check over them cannot work: the corrected text QUOTES each
retracted claim in order to retract it, so any search that finds the old
wording finds the new wording too. Reshaping the documentation to dodge a
grep would make it worse, and a check that cannot tell an assertion from its
denial is not worth the line. What is testable is tested — the schema policy
and the manifest, below.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_EVENTS = _REPO / "common/events"


def _module_calls_bus_subscribe(path: pathlib.Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "subscribe"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bus"
        ):
            return True
    return False


def test_every_subscribing_helper_module_is_registered_with_the_generator() -> None:
    """A shared registrar the generator cannot see is a topic nobody provisions.

    ``events_manifest.json`` is what caura-enterprise's
    ``check_pubsub_provisioning.py`` reads. A module that subscribes but is
    absent from the generator's registry contributes nothing to it, and the
    existing ``_DIRECT_SUBSCRIBES`` guard only covers subscribes written
    directly in a service's consumer file — so a helper falls between the two.
    That is exactly how ``caura.org.suppression-changed`` went missing.
    """
    import scripts.gen_events_manifest as gen

    subscribing = {
        path.stem
        for path in sorted(_EVENTS.glob("*.py"))
        if _module_calls_bus_subscribe(path)
    }
    assert subscribing, (
        "no module in common/events calls bus.subscribe — the scan is broken"
    )

    registered = {
        register.__module__.rsplit(".", 1)[-1]
        for registrars in gen._SHARED_REGISTRARS.values()
        for register in registrars
    }
    missing = sorted(subscribing - registered)
    assert not missing, (
        f"these common/events modules subscribe but no service registers them in "
        f"_SHARED_REGISTRARS: {missing}. Their topics will be absent from "
        "events_manifest.json, and the enterprise provisioning check will not "
        "require a subscription for them."
    )


def test_the_suppression_topic_reaches_the_manifest() -> None:
    """The specific omission, asserted on the generated output rather than the
    committed file — so this fails if the generator regresses even when the
    JSON on disk still happens to be right."""
    import scripts.gen_events_manifest as gen

    manifest = gen.build_manifest()
    worker = manifest["services"]["core-worker"]
    assert "caura.org.suppression-changed" in worker, worker


def _consumer_schema_modules() -> list[pathlib.Path]:
    return [
        p
        for p in sorted(_EVENTS.glob("*.py"))
        if "ConfigDict(" in p.read_text() and p.name != "base.py"
    ]


@pytest.mark.parametrize("path", _consumer_schema_modules(), ids=lambda p: p.name)
def test_no_wire_schema_forbids_extra_fields(path: pathlib.Path) -> None:
    """``extra="forbid"`` on a consumer schema turns a rolling deploy into data loss.

    The consumer ack-drops on ``ValidationError`` — deliberately, so a poison
    message cannot nack-loop — which means a publisher shipped first with any
    new optional field has every message of that topic acknowledged and thrown
    away for the length of the deploy.
    """
    assert 'extra="forbid"' not in path.read_text(), (
        f'{path.name} forbids extra fields on a wire schema; use extra="ignore" '
        "(see MemoryEnrichRequest for the rationale) so an additive publisher "
        "field does not ack-drop the topic during a rolling deploy."
    )


def test_the_embed_request_tolerates_a_field_it_does_not_know() -> None:
    """The behaviour, not just the config flag."""
    from common.events.memory_embed_request import MemoryEmbedRequest

    payload = {
        "memory_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "t1",
        "content": "hello",
        # What a publisher shipped one deploy ahead would add.
        "some_future_field": "value",
    }
    request = MemoryEmbedRequest(**payload)
    assert request.tenant_id == "t1"
    assert not hasattr(request, "some_future_field")
