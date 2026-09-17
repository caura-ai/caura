"""One failing relation upsert used to silently disable two later features.

``process_entity_extraction`` upserts each extracted relation in a loop. The
loop had no per-relation guard, so a single failure threw out of the whole
function into the outer handler, which logs::

    Entity extraction failed for memory <id> (non-fatal)

"Non-fatal" is true of the request and false of everything after the loop:

* the **A65 predicate write-back** never runs, so the row keeps a NULL
  ``predicate`` — permanently, because nothing re-extracts it; and
* the ``Trigger.ENTITY`` fire never runs, which is the ONLY thing that invokes
  A40's deterministic RDF pass.

So one transient storage error on one relation, out of dozens for that memory,
quietly removed that row from the deterministic contradiction path for good and
reported success. This was observed on a real stack, not imagined: a storage 500
on ``POST /entities/relations`` reproduced it exactly.

Each upsert is now guarded individually — the pattern ``subject_writeback`` and
``predicate_writeback`` in the same function already use.
"""

import inspect

import pytest

from core_api.services import entity_extraction_worker as w

pytestmark = pytest.mark.unit


def _code_only(src: str) -> str:
    """``src`` with comment lines removed.

    Load-bearing. These assertions slice the source between landmarks, and this
    change ADDS comments that name ``Trigger.ENTITY`` and the predicate
    write-back in prose. Slicing raw source finds the comment first and the
    tests then pass or fail on what the comment says rather than what the code
    does — which is a way of testing nothing at all.
    """
    return "\n".join(
        line for line in src.split("\n") if not line.lstrip().startswith("#")
    )


def _relation_loop_source() -> str:
    """The upsert loop plus everything the old cascade skipped."""
    src = _code_only(inspect.getsource(w.process_entity_extraction))
    start = src.index("for rel in graph.relations:")
    return src[start:]


def test_each_relation_upsert_is_guarded():
    """The fix itself. An unguarded ``await upsert_relation(`` inside the loop
    is what let one failure abort the rest of extraction."""
    loop = _relation_loop_source()
    body = loop[: loop.index("predicate_writeback")]
    assert "try:" in body, "relation upsert must be guarded per relation"
    assert "except Exception:" in body


def test_a_failed_relation_does_not_skip_the_predicate_writeback():
    """The first thing the cascade used to take out. Ordering is the assertion:
    the guard has to sit BEFORE the write-back in the same function, or a throw
    still jumps past it."""
    src = _code_only(inspect.getsource(w.process_entity_extraction))
    assert src.index("for rel in graph.relations:") < src.index("predicate_writeback")
    loop_to_writeback = src[
        src.index("for rel in graph.relations:") : src.index("predicate_writeback")
    ]
    assert "except Exception:" in loop_to_writeback


def test_partial_failure_is_counted_not_just_logged_per_item():
    """A degrading entity graph should read as one number, not N scattered
    warnings that nobody aggregates."""
    loop = _relation_loop_source()
    assert "rel_failed" in loop
    assert "relation_upsert_partial" in loop


def test_successful_relations_still_counted_when_a_sibling_fails():
    """``rel_count`` must increment only on success, so the completion log does
    not report relations that were never written."""
    loop = _relation_loop_source()
    body = loop[: loop.index("predicate_writeback")]
    # the increment sits inside the try, after the await
    assert body.index("await upsert_relation(") < body.index("rel_count += 1")
    assert body.index("rel_count += 1") < body.index("except Exception:")


def test_the_guard_is_inside_the_loop_not_around_it():
    """Non-vacuity matters here. The function already has an outer
    ``try/except`` and the predicate write-back has its own, so a test that
    merely looks for "an except somewhere after the loop" passes with this fix
    REVERTED and asserts nothing.

    The load-bearing property: a ``try:`` opens BEFORE the awaited upsert and
    inside the loop body, so one failure costs one relation instead of the
    remaining relations plus every stage after the loop. Path C's survival is
    covered behaviourally below, which is the assertion that actually fails
    without the guard.
    """
    loop = _relation_loop_source()
    body = loop[: loop.index("predicate_writeback")]
    assert "try:" in body
    assert body.index("try:") < body.index("await upsert_relation(")


# ── behavioural: the cascade itself ───────────────────────────────────────
#
# The assertions above pin the SHAPE of the guard. These two run the function
# with one relation upsert failing and check that the two stages the cascade
# used to take out still happen — which is the actual contract.

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
from uuid import uuid4  # noqa: E402

from core_api.services.entity_extraction_worker import (  # noqa: E402
    process_entity_extraction,
)
from tests.conftest import close_scheduled_coro  # noqa: E402

# Reuse the fixtures the bulk-extraction suite already exercises, rather than
# re-deriving the mock shapes here — a divergent fixture is how a behavioural
# test ends up asserting against a graph the worker never actually builds.
from tests.test_p1_entity_extraction_bulk import (  # noqa: E402
    _build_sc_mock,
    _config,
    _entity,
    _graph,
)


def _relation(frm, rel_type, to):
    r = MagicMock()
    r.from_entity = frm
    r.relation_type = rel_type
    r.to_entity = to
    return r


def _sc():
    sc = _build_sc_mock(
        resolve_returns=[None, None],
        # ``entity_id``, NOT ``id`` — the worker reads ``r["entity_id"]`` and
        # skips the row otherwise, which silently leaves ``name_to_id`` empty
        # and the relation loop unreachable. A fixture that gets this wrong
        # makes the test pass for the wrong reason: zero relations attempted.
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


@pytest.mark.asyncio
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_one_failing_relation_does_not_abort_the_others(
    mock_resolve, mock_extract, mock_sc_factory, _embed, mock_rel, _log
):
    """The first relation blows up; the second must still be attempted.

    Before the guard, the throw left the loop on iteration one and every
    remaining relation was silently dropped along with it.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("maya"), _entity("boston", "location", "object")],
        [
            _relation("maya", "lives_in", "boston"),
            _relation("maya", "works_at", "boston"),
        ],
    )
    mock_sc_factory.return_value = _sc()
    mock_rel.side_effect = [RuntimeError("storage 500"), None]

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="Maya lives in Boston",
            memory_type="episodic",
        )

    assert mock_rel.call_count == 2, "second relation must still be attempted"


@pytest.mark.asyncio
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_failing_relation_still_reaches_the_contradiction_trigger(
    mock_resolve, mock_extract, mock_sc_factory, _embed, mock_rel, _log
):
    """The consequence that mattered. ``Trigger.ENTITY`` is the only caller of
    A40's deterministic RDF pass, so losing it removes that memory from the
    non-stochastic contradiction path permanently — nothing re-extracts it."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("maya"), _entity("boston", "location", "object")],
        [_relation("maya", "lives_in", "boston")],
    )
    mock_sc_factory.return_value = _sc()
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
