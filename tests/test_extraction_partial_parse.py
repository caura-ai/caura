"""One malformed item must not discard the whole extraction.

#788 fixed a single field; the boundary is what actually sets the blast radius.
For why, see ``_parse_graph_lenient``'s docstring.

The parametrized cases below enumerate the fields that were still exposed —
documenting the surface, not defining the contract. The contract is the
boundary behaviour, which holds for any field a future model makes required.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from core_api.services.entity_extraction import (
    ExtractedGraph,
    _parse_graph_lenient,
)

pytestmark = [pytest.mark.unit]


def _ok_entity(name: str = "anna") -> dict:
    return {"canonical_name": name, "entity_type": "person", "role": "subject"}


def _ok_relation() -> dict:
    return {"from_entity": "anna", "relation_type": "works_on", "to_entity": "helios"}


def _ok_mention(surface: str = "Anna") -> dict:
    return {"surface": surface, "cluster_id": 0, "entity_canonical": "anna"}


@pytest.mark.parametrize(
    "field,items",
    [
        # The exact #788 shape, reproduced on each sibling field that was
        # still exposed. A null in a non-optional slot.
        ("entities", [_ok_entity("anna"), {**_ok_entity("bob"), "role": None}]),
        ("entities", [_ok_entity("anna"), {**_ok_entity("bob"), "entity_type": None}]),
        (
            "entities",
            [_ok_entity("anna"), {**_ok_entity("bob"), "canonical_name": None}],
        ),
        ("relations", [_ok_relation(), {**_ok_relation(), "relation_type": None}]),
        ("relations", [_ok_relation(), {**_ok_relation(), "from_entity": None}]),
        ("relations", [_ok_relation(), {**_ok_relation(), "to_entity": None}]),
        ("mentions", [_ok_mention("Anna"), {**_ok_mention("She"), "surface": None}]),
    ],
)
def test_one_bad_item_does_not_discard_its_siblings(field, items) -> None:
    """The good item in the same list must survive the bad one."""
    raw = {field: items}

    # Control: the atomic parse this replaces loses everything.
    with pytest.raises(ValidationError):
        ExtractedGraph(**raw)

    graph, dropped = _parse_graph_lenient(raw)

    assert dropped == {field: 1}, (
        f"expected exactly one dropped {field}; got {dropped!r}"
    )
    kept = getattr(graph, field)
    assert len(kept) == len(items) - 1, (
        f"the well-formed {field} item must survive; kept {kept!r}"
    )


def test_a_bad_item_in_one_list_does_not_touch_the_others() -> None:
    """Blast radius is the item, not the payload."""
    raw = {
        "entities": [_ok_entity("anna"), {**_ok_entity("bob"), "role": None}],
        "relations": [_ok_relation()],
        "mentions": [_ok_mention()],
    }

    graph, dropped = _parse_graph_lenient(raw)

    assert dropped == {"entities": 1}
    assert len(graph.entities) == 1
    assert len(graph.relations) == 1, "a bad entity must not drop a good relation"
    assert len(graph.mentions) == 1, "a bad entity must not drop a good mention"


@pytest.mark.parametrize("junk", [None, "a string", 5, ["nested"]])
def test_non_mapping_items_are_dropped(junk) -> None:
    """Junk of any shape is a drop, not an escape.

    ``model_validate`` raises ValidationError for a non-mapping too, which is
    why one narrow except suffices; ``model(**item)`` would raise TypeError
    here and force a broader catch.
    """
    raw = {"entities": [_ok_entity(), junk]}

    graph, dropped = _parse_graph_lenient(raw)

    assert dropped == {"entities": 1}
    assert len(graph.entities) == 1


def test_registry_covers_every_graph_list_field() -> None:
    """The item-model registry must cover every list field on the graph.

    A field absent from ``_GRAPH_ITEM_MODELS`` is never walked: it silently
    defaults to empty and nothing reports it. This is the check that makes
    that omission loud, since no runtime behaviour does.
    """
    from core_api.services.entity_extraction import _GRAPH_ITEM_MODELS

    list_fields = {
        name
        for name, f in ExtractedGraph.model_fields.items()
        if str(f.annotation).startswith("list[")
    }
    assert list_fields, "sanity: ExtractedGraph should have list fields"
    assert set(_GRAPH_ITEM_MODELS) == list_fields, (
        "every list field on ExtractedGraph needs an item model, or it is "
        f"silently skipped; registry={set(_GRAPH_ITEM_MODELS)!r} fields={list_fields!r}"
    )


def test_a_non_list_field_is_dropped_wholesale() -> None:
    """A non-list where a list belongs has no items to walk."""
    graph, dropped = _parse_graph_lenient({"entities": {"canonical_name": "anna"}})

    assert dropped == {"entities": 1}
    assert graph.entities == []


def test_clean_payload_is_unchanged_and_reports_nothing_dropped() -> None:
    """Control: tolerance must not alter a well-formed payload."""
    raw = {
        "entities": [_ok_entity("anna"), _ok_entity("bob")],
        "relations": [_ok_relation()],
        "mentions": [_ok_mention()],
    }

    graph, dropped = _parse_graph_lenient(raw)

    assert dropped == {}
    assert [e.canonical_name for e in graph.entities] == ["anna", "bob"]
    assert graph.relations[0].relation_type == "works_on"
    assert graph.mentions[0].surface == "Anna"
    # Byte-identical to the atomic parse when nothing is malformed.
    assert graph == ExtractedGraph(**raw)


def test_empty_payload_stays_empty() -> None:
    """ "No entities found" is a legitimate answer, not a failure."""
    graph, dropped = _parse_graph_lenient(
        {"entities": [], "relations": [], "mentions": []}
    )

    assert dropped == {}
    assert graph == ExtractedGraph()


def test_missing_fields_default_rather_than_drop() -> None:
    """Back-compat: a pre-A5b payload without ``mentions`` is not a drop."""
    graph, dropped = _parse_graph_lenient({"entities": [_ok_entity()]})

    assert dropped == {}
    assert graph.mentions == []
    assert len(graph.entities) == 1


# ---------------------------------------------------------------------------
# Call-site behaviour: what _do_extract does with the salvage result
# ---------------------------------------------------------------------------


async def _extract_with_payload(payload):
    """Run the real extraction path against a stubbed provider response."""
    from common.llm.providers.openai import OpenAILLMProvider
    from core_api.services.entity_extraction import extract_entities_from_content

    async def _fake_complete_json(self, prompt: str, **kwargs):
        return payload

    with (
        patch.object(OpenAILLMProvider, "complete_json", _fake_complete_json),
        patch(
            "common.llm.registry.get_llm_provider",
            return_value=OpenAILLMProvider(api_key="sk-test", model="gpt-test"),
        ),
        patch(
            "core_api.services.entity_extraction.settings.entity_extraction_provider",
            "openai",
        ),
    ):
        return await extract_entities_from_content(
            "Anna Bergstrom ships Vermillion", "fact"
        )


@pytest.mark.asyncio
async def test_partial_payload_survives_end_to_end() -> None:
    """A payload with one bad entity must still yield the good one.

    This is the whole point: before, the fallback chain ran and the result
    came from the regex heuristic, losing the LLM's real extraction.
    """
    graph = await _extract_with_payload(
        {
            "entities": [_ok_entity("anna"), {**_ok_entity("bob"), "role": None}],
            "relations": [_ok_relation()],
            "mentions": [],
        }
    )

    names = [e.canonical_name for e in graph.entities]
    assert "anna" in names, f"the well-formed entity must survive; got {names!r}"
    assert len(graph.relations) == 1


@pytest.mark.asyncio
async def test_total_loss_falls_back_rather_than_returning_empty() -> None:
    """If NOTHING survives, the fallback chain must still get its turn.

    Returning an empty graph would silently assert "no entities in this
    content" — indistinguishable from a legitimately empty extraction, and it
    would rob the alternative provider (a different model, which may well
    parse) of its chance. ``_fake_extract`` finds capitalised multi-word
    phrases, so the fallback is observable.
    """
    graph = await _extract_with_payload(
        {
            "entities": [{**_ok_entity("bob"), "role": None}],
            "relations": [],
            "mentions": [],
        }
    )

    assert graph.entities, (
        "an all-dropped payload must reach the fallback, not return empty; "
        f"got {graph!r}"
    )
    assert any("anna bergstrom" == e.canonical_name for e in graph.entities), (
        f"expected the regex fallback's extraction; got {[e.canonical_name for e in graph.entities]!r}"
    )


@pytest.mark.asyncio
async def test_legitimately_empty_payload_stays_empty_end_to_end() -> None:
    """Control: an empty payload is NOT a total loss and must not fall back.

    Without this, "drop nothing, keep nothing" and "drop everything" would be
    indistinguishable, and every genuinely entity-free memory would burn the
    fallback chain.
    """
    graph = await _extract_with_payload(
        {"entities": [], "relations": [], "mentions": []}
    )

    assert graph.entities == [], (
        f"an empty extraction must be respected, not retried into the fallback; got {graph!r}"
    )


@pytest.mark.asyncio
async def test_surviving_mention_does_not_suppress_the_fallback() -> None:
    """A salvaged mention must not stand in for salvaged entities.

    ``mentions`` has no consumer that persists anything, and the worker
    early-returns on an entity-less graph — so if a surviving mention
    suppressed the fallback, the extraction would be lost with no audit row.
    That is the failure this predicate exists to prevent, reached through the
    one field with no reader.
    """
    graph = await _extract_with_payload(
        {
            "entities": [{**_ok_entity("bob"), "role": None}],
            "relations": [],
            "mentions": [_ok_mention("Anna")],
        }
    )

    assert any(e.canonical_name == "anna bergstrom" for e in graph.entities), (
        "all entities dropped must reach the fallback even when a mention survived; "
        f"got {[e.canonical_name for e in graph.entities]!r}"
    )


@pytest.mark.asyncio
async def test_malformed_mention_alone_does_not_trigger_the_fallback() -> None:
    """The inverse: a bad mention must not override a correct empty extraction.

    With no entities offered, "no entities in this content" is the LLM's
    answer and it is probably right. Falling back here would let the regex
    heuristic invent entities the model correctly reported as absent.
    """
    graph = await _extract_with_payload(
        {
            "entities": [],
            "relations": [],
            "mentions": [{**_ok_mention("She"), "surface": None}],
        }
    )

    assert graph.entities == [], (
        "a malformed mention must not send a correct empty extraction to the "
        f"regex fallback; got {[e.canonical_name for e in graph.entities]!r}"
    )


def test_extraction_shape_error_is_a_valueerror_and_carries_no_payload() -> None:
    """The raise type is load-bearing, and it must not leak content.

    ``call_with_retry`` catches broad ``Exception``, so the end-to-end tests
    above pass regardless of what type is raised — they cannot see this. It is
    asserted directly because the pending #788 follow-up (classify shape
    failures as non-retryable) needs one narrow type to key on, and because an
    earlier draft used ``ProviderResponseShapeError``, whose contract STORES
    and renders up to 1 KiB of ``content`` — which here would be memory text.
    """
    from core_api.services.entity_extraction import ExtractionShapeError

    # Subclassing ValueError is what keeps today's fallback behaviour unchanged.
    assert issubclass(ExtractionShapeError, ValueError)

    err = ExtractionShapeError(
        "entity extraction produced no usable entities (dropped={'entities': 1})"
    )
    rendered = str(err)
    assert "dropped=" in rendered, "counts are the diagnostic value; keep them"
    # No constructor slot for a payload, and nothing renders one.
    assert not hasattr(err, "content"), (
        "this error must not carry a response payload — that is the transport-layer "
        "ProviderResponseShapeError's contract, and it renders content into tracebacks"
    )
