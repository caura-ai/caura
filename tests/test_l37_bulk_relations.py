"""L-37 — the extraction worker batched everything except its relations.

``process_entity_extraction`` collapses its entity work into three storage
calls: one ``bulk_resolve_entities``, one ``bulk_upsert_entities``, one
``bulk_upsert_entity_links``. Then it reached the relations and went back to a
sequential ``await`` per edge, so a memory with N relations paid N serial HTTP
round-trips after the rest of its extraction had been reduced to three.

The count is the assertion. A test that only checked "the relations were
written" passes on the pre-fix code, because the pre-fix code wrote them — one
POST at a time.

What must NOT change is the failure isolation. Before #1495 a single storage
error on one relation out of dozens threw out of the whole function and skipped
the A65 predicate write-back and the ``Trigger.ENTITY`` fire — which is the only
caller of A40's deterministic RDF pass — leaving that memory out of the
non-stochastic contradiction path permanently, while logging "non-fatal". So the
batch endpoint reports PER ITEM, and the tests below pin both halves: N edges
cost one call, and one refused edge costs one edge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services.entity_extraction_worker import process_entity_extraction
from tests.conftest import close_scheduled_coro
from tests.test_p1_entity_extraction_bulk import (
    _build_sc_mock,
    _config,
    _entity,
    _graph,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _relation(frm: str, rel_type: str, to: str) -> MagicMock:
    r = MagicMock()
    r.from_entity = frm
    r.relation_type = rel_type
    r.to_entity = to
    return r


def _sc_for(names: list[str]) -> MagicMock:
    """A storage mock that mints one new entity per name."""
    sc = _build_sc_mock(
        resolve_returns=[None] * len(names),
        # ``entity_id``, NOT ``id`` — the worker skips a row without it, which
        # leaves ``name_to_id`` empty and makes the relation block unreachable.
        # A fixture that gets this wrong passes for the wrong reason: zero
        # relations attempted, zero calls, assertion satisfied.
        upsert_returns=[
            {"input_idx": i, "entity_id": str(uuid4()), "action": "created"}
            for i in range(len(names))
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


def _worker_patches(fn):
    """The five patches every test here needs, applied in one place.

    Applied in the listed order, so ``resolve_config`` ends up INNERMOST and
    its mock is the first injected argument — the same bottom-up order a
    stacked ``@patch`` gives. Every test below takes them as
    ``(mock_resolve, mock_extract, mock_sc_factory, _embed, _log)``, with the
    separately stacked ``bulk_upsert_relations`` mock last.
    """
    for deco in (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new_callable=AsyncMock,
        ),
        patch(
            "core_api.services.entity_extraction_worker.extract_entities_from_content",
            new_callable=AsyncMock,
        ),
        patch("core_api.services.entity_extraction_worker.get_storage_client"),
        patch(
            "core_api.services.entity_extraction_worker.get_embedding",
            new_callable=AsyncMock,
        ),
        patch(
            "core_api.services.entity_extraction_worker.log_action",
            new_callable=AsyncMock,
        ),
    ):
        fn = deco(fn)
    return fn


async def _run(memory_id=None) -> None:
    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=memory_id or uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="Maya works at Acme in Boston with Raj",
            memory_type="episodic",
        )


@patch(
    "core_api.services.entity_extraction_worker.bulk_upsert_relations",
    new_callable=AsyncMock,
)
@_worker_patches
async def test_n_relations_cost_one_storage_call(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """Four relations, ONE call. Pre-fix this was four.

    The number is the whole point: the pre-fix worker wrote every one of these
    relations correctly, just not in one round-trip.
    """
    names = ["maya", "acme", "boston", "raj"]
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity(names[0])] + [_entity(n, "organization", "object") for n in names[1:]],
        [
            _relation("maya", "works_at", "acme"),
            _relation("maya", "lives_in", "boston"),
            _relation("maya", "reports_to", "raj"),
            _relation("acme", "located_in", "boston"),
        ],
    )
    mock_sc_factory.return_value = _sc_for(names)
    mock_rel.return_value = [True, True, True, True]

    await _run()

    assert mock_rel.await_count == 1, (
        f"four relations must cost ONE storage call, not {mock_rel.await_count}"
    )
    sent = mock_rel.await_args.args[0]
    assert len(sent) == 4, "all four relations must travel in that one call"
    assert [s.relation_type for s in sent] == [
        "works_at",
        "lives_in",
        "reports_to",
        "located_in",
    ]


@patch(
    "core_api.services.entity_extraction_worker.bulk_upsert_relations",
    new_callable=AsyncMock,
)
@_worker_patches
async def test_relations_with_an_unresolvable_endpoint_are_not_sent(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """An edge naming an entity that never landed is dropped before the wire.

    Same silent skip the sequential loop did (``if from_id and to_id``). Worth
    pinning because the batch now has to decide this up-front rather than per
    iteration, and sending it instead would spend a round-trip earning a
    guaranteed ``fk_violation``.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("maya"), _entity("acme", "organization", "object")],
        [
            _relation("maya", "works_at", "acme"),
            _relation("maya", "lives_in", "atlantis"),  # never extracted
        ],
    )
    mock_sc_factory.return_value = _sc_for(["maya", "acme"])
    mock_rel.return_value = [True]

    await _run()

    sent = mock_rel.await_args.args[0]
    assert [s.relation_type for s in sent] == ["works_at"]


@patch(
    "core_api.services.entity_extraction_worker.bulk_upsert_relations",
    new_callable=AsyncMock,
)
@_worker_patches
async def test_no_relations_means_no_call_at_all(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """The common case. An extraction with no usable edges must not spend a
    round-trip to say so — the sequential loop's zero iterations cost nothing
    and neither may the batch."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("maya")], [])
    mock_sc_factory.return_value = _sc_for(["maya"])

    await _run()

    mock_rel.assert_not_awaited()


@patch(
    "core_api.services.entity_extraction_worker.bulk_upsert_relations",
    new_callable=AsyncMock,
)
@_worker_patches
async def test_one_refused_relation_still_lets_the_predicate_writeback_run(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """#1495's guarantee, under the batch.

    Storage refuses item 0 and accepts item 1. That verdict arrives as a
    per-item ``False``, not an exception, so everything downstream — and in
    particular the A65 predicate write-back — must still run.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("maya"), _entity("boston", "location", "object")],
        [
            _relation("maya", "lives_in", "boston"),
            _relation("maya", "works_at", "boston"),
        ],
    )
    sc = _sc_for(["maya", "boston"])
    mock_sc_factory.return_value = sc
    mock_rel.return_value = [False, True]

    await _run()

    # ``lives_in`` is the only one of the two that canonicalises, so the
    # write-back is unambiguous and must have fired.
    sc.set_predicate_if_null.assert_awaited_once()
    assert sc.set_predicate_if_null.await_args.kwargs["predicate"] == "lives_in"


@patch(
    "core_api.services.entity_extraction_worker.bulk_upsert_relations",
    new_callable=AsyncMock,
)
@_worker_patches
async def test_a_whole_batch_failure_still_reaches_the_contradiction_trigger(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """The other failure mode the batch introduces: the CALL fails, not an item.

    A storage 500 or a timeout now costs every relation at once, which is
    precisely why it must be guarded — an unguarded raise here reproduces the
    #1495 cascade in one step, taking out the predicate write-back and the
    ``Trigger.ENTITY`` fire that is the only caller of A40's RDF pass.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("maya"), _entity("boston", "location", "object")],
        [_relation("maya", "lives_in", "boston")],
    )
    mock_sc_factory.return_value = _sc_for(["maya", "boston"])
    mock_rel.side_effect = RuntimeError("storage 500")

    scheduled = []

    def _capture(coro, *a, **k):
        scheduled.append(coro)
        return close_scheduled_coro(coro, *a, **k)

    with patch("core_api.tasks.track_task", side_effect=_capture):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="Maya lives in Boston",
            memory_type="episodic",
        )

    assert scheduled, "contradiction detection must still be scheduled"


# ── the round-trip count, measured at the wire ────────────────────────────
#
# The tests above patch ``bulk_upsert_relations`` and count calls to it, which
# is the right granularity for the worker's own bookkeeping but cannot fail on
# the pre-fix code for the right reason: that name does not exist there, so the
# patch target does. This one patches nothing between the worker and the storage
# CLIENT, so it counts the HTTP round-trips themselves and reads the same on
# both versions — four ``create_relation`` calls before, one
# ``bulk_create_relations`` call after.


@patch("core_api.services.entity_service.get_storage_client")
@_worker_patches
async def test_four_relations_cost_one_http_not_four(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_service_sc_factory
):
    """N relations, ONE storage round-trip.

    The pre-fix worker issued ``POST /entities/relations`` once per edge, in
    sequence, after having already collapsed its entity work into three calls.
    Counted here at the storage client, where the round-trips actually are.
    """
    names = ["maya", "acme", "boston", "raj"]
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity(names[0])] + [_entity(n, "organization", "object") for n in names[1:]],
        [
            _relation("maya", "works_at", "acme"),
            _relation("maya", "lives_in", "boston"),
            _relation("maya", "reports_to", "raj"),
            _relation("acme", "located_in", "boston"),
        ],
    )
    sc = _sc_for(names)
    # The singular route is the thing that must go unused; the batch route
    # answers with one landed slot per item.
    sc.create_relation = AsyncMock()
    sc.bulk_create_relations = AsyncMock(
        return_value=[
            {"input_idx": i, "relation": {"id": str(uuid4())}} for i in range(4)
        ]
    )
    mock_sc_factory.return_value = sc
    # The relation upsert reaches storage through ``entity_service``'s own
    # client handle, not the worker's — both have to resolve to this mock or
    # the call escapes to the real one and the count measures nothing.
    mock_service_sc_factory.return_value = sc

    await _run()

    assert sc.create_relation.await_count == 0, (
        f"the per-relation POST must be gone entirely, saw "
        f"{sc.create_relation.await_count} call(s)"
    )
    assert sc.bulk_create_relations.await_count == 1, (
        f"four relations must cost ONE round-trip, saw "
        f"{sc.bulk_create_relations.await_count}"
    )
    items = sc.bulk_create_relations.await_args.kwargs["items"]
    assert [i["relation_type"] for i in items] == [
        "works_at",
        "lives_in",
        "reports_to",
        "located_in",
    ]
    assert [i["input_idx"] for i in items] == [0, 1, 2, 3]
