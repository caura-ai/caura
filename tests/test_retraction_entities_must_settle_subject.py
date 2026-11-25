"""The retraction judge reads entity rows only where they settle the subject.

Path C re-judges a content-route verdict with both rows' resolved entities. Its
prompt reads "different ``entity_id``" as "different subject" and clears the
verdict on that alone. That is right for two person rows (two people called
Priya) and wrong for everything else: extraction files one thing under two rows
all the time ("support team" / "support hours", "marketing site cdn" / "content
delivery network in front of the marketing site"), so a real correction was
marked and then unmarked.

Where the rows do not settle the subject, the same single call now judges the
two statements themselves.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services.contradiction_detector import _entities_settle_subject
from tests.test_a4_13_path_c_retraction import _mock_sc_with_retraction_setup

pytestmark = pytest.mark.unit


def _subject(entity_id: str, entity_type: str) -> dict:
    return {
        "name": "x",
        "entity_type": entity_type,
        "role": "subject",
        "entity_id": entity_id,
    }


def test_shared_subject_row_settles_it() -> None:
    assert _entities_settle_subject(
        [_subject("e1", "project")], [_subject("e1", "project")]
    )


def test_two_person_rows_settle_it() -> None:
    assert _entities_settle_subject(
        [_subject("e1", "person")], [_subject("e2", "person")]
    )


@pytest.mark.parametrize(
    ("new_type", "old_type"),
    [("project", "project"), ("concept", "organization"), ("person", "project")],
)
def test_differing_rows_of_other_types_do_not(new_type: str, old_type: str) -> None:
    assert not _entities_settle_subject(
        [_subject("e1", new_type)], [_subject("e2", old_type)]
    )


def test_a_side_without_a_subject_row_does_not() -> None:
    mentioned = {
        "name": "x",
        "entity_type": "person",
        "role": "object",
        "entity_id": "e1",
    }
    assert not _entities_settle_subject([mentioned], [_subject("e1", "person")])
    assert not _entities_settle_subject([], [_subject("e1", "person")])


def _non_person_rows(sc: AsyncMock) -> None:
    """Re-type the helper's two subject rows as two differently named things."""

    async def get_entity(eid: str, tenant_id: str) -> dict | None:
        return {
            "id": eid,
            "canonical_name": f"thing {eid[:4]}",
            "entity_type": "concept",
        }

    sc.get_entity = AsyncMock(side_effect=get_entity)


async def _run(
    sc: AsyncMock, new_id, *, plain_verdict: tuple[bool, float]
) -> tuple[AsyncMock, AsyncMock]:
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.90),
        ) as entity_aware,
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new_callable=AsyncMock,
            return_value=plain_verdict,
        ) as plain,
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")
    return entity_aware, plain


@pytest.mark.asyncio
async def test_differing_rows_do_not_clear_a_verdict_the_statements_support() -> None:
    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)
    _non_person_rows(sc)

    entity_aware, plain = await _run(sc, new_id, plain_verdict=(True, 0.90))

    entity_aware.assert_not_awaited()
    plain.assert_awaited_once()
    assert not any(
        c.args == (str(cand_id), "active")
        for c in sc.update_memory_status.call_args_list
    )


@pytest.mark.asyncio
async def test_differing_rows_still_clear_a_verdict_the_statements_do_not_support() -> (
    None
):
    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)
    _non_person_rows(sc)

    entity_aware, plain = await _run(sc, new_id, plain_verdict=(False, 0.90))

    entity_aware.assert_not_awaited()
    plain.assert_awaited_once()
    assert any(
        c.args == (str(cand_id), "active")
        for c in sc.update_memory_status.call_args_list
    )


@pytest.mark.asyncio
async def test_person_rows_keep_the_entity_aware_judge() -> None:
    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    entity_aware, plain = await _run(sc, new_id, plain_verdict=(True, 0.90))

    entity_aware.assert_awaited_once()
    plain.assert_not_awaited()
    assert any(
        c.args == (str(cand_id), "active")
        for c in sc.update_memory_status.call_args_list
    )
