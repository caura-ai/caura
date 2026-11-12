"""Reviewed effects and their decision are one PostgreSQL transaction.

These database cases run in CI; no live-data repair is involved.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def memory(client, tenant):
    response = await client.post(PREFIX + "/memories", json=_memory_payload(tenant, None))
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
async def pair(client):
    tenant = "review-" + uuid4().hex
    old, new = await memory(client, tenant), await memory(client, tenant)
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET created_at = '2026-10-01T00:00:00Z' WHERE id = :id"), {"id": old}
        )
        await session.execute(
            text("UPDATE memories SET created_at = '2026-10-02T00:00:00Z' WHERE id = :id"), {"id": new}
        )
    response = await client.post(
        PREFIX + "/memories/conflicts",
        json={
            "tenant_id": tenant,
            "new_memory_id": new,
            "old_memory_id": old,
            "relationship": "exact_value",
            "action": "mark_disputed",
            "metadata": {"effect_applied": False, "author_trust": {"new_memory": 1, "old_memory": 3}},
        },
    )
    assert response.status_code == 200, response.text
    return {"tenant": tenant, "old": old, "new": new, "conflict": response.json()["id"]}


async def review(client, pair, action="supersede"):
    body = {"tenant_id": pair["tenant"], "review_status": "resolved", "resolved_by": "reviewer"}
    if action is not None:
        body["resolution_action"] = action
    return await client.patch(PREFIX + f"/memories/memory-conflicts/{pair['conflict']}/resolve", json=body)


async def state(pair):
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id::text, status, supersedes_id::text, verification_status FROM memories "
                        "WHERE tenant_id = :tenant"
                    ),
                    {"tenant": pair["tenant"]},
                )
            )
            .mappings()
            .all()
        )
        conflict = (
            (
                await session.execute(
                    text(
                        "SELECT review_status, resolution_action, metadata, action FROM memory_conflicts "
                        "WHERE id = :id AND tenant_id = :tenant"
                    ),
                    {"id": pair["conflict"], "tenant": pair["tenant"]},
                )
            )
            .mappings()
            .one()
        )
        return {r["id"]: dict(r) for r in rows}, dict(conflict)


@pytest.mark.parametrize("action,status", [("replace", "conflicted"), ("supersede", "outdated")])
async def test_resolving_an_unapplied_dispute_applies_the_selected_verdict(client, pair, action, status):
    response = await review(client, pair, action)
    assert response.status_code == 200, response.text
    rows, conflict = await state(pair)
    assert rows[pair["old"]]["status"] == status
    assert rows[pair["new"]]["supersedes_id"] == pair["old"]
    assert rows[pair["new"]]["status"] == "active"
    assert conflict["review_status"] == "resolved" and conflict["resolution_action"] == action
    assert conflict["action"] == "mark_disputed"
    assert conflict["metadata"]["effect_applied"] is False
    assert conflict["metadata"]["review_effect_applied"] is True
    assert conflict["metadata"]["author_trust"] == {"new_memory": 1, "old_memory": 3}


async def test_an_omitted_choice_applies_and_records_the_stored_proposal(client, pair):
    async with get_session() as session:
        await session.execute(
            text("UPDATE memory_conflicts SET action = 'supersede' WHERE id = :id"), {"id": pair["conflict"]}
        )
    response = await review(client, pair, None)
    assert response.status_code == 200, response.text
    rows, conflict = await state(pair)
    assert rows[pair["old"]]["status"] == "outdated"
    assert conflict["resolution_action"] == "supersede"


@pytest.mark.parametrize("action", ["scope", "merge", "split_entity", "downweight", "ask"])
async def test_actions_needing_additional_input_leave_the_review_pending(client, pair, action):
    response = await review(client, pair, action)
    assert response.status_code == 400, response.text
    rows, conflict = await state(pair)
    assert conflict["review_status"] == "pending"
    assert rows[pair["old"]]["status"] == rows[pair["new"]]["status"] == "active"


@pytest.mark.parametrize("status", ["deleted", "quarantined", "archived"])
async def test_unavailable_pair_members_are_never_revived_by_review(client, pair, status):
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE memories SET status = :status, deleted_at = CASE WHEN :status = 'deleted' "
                "THEN now() ELSE NULL END WHERE id = :id"
            ),
            {"status": status, "id": pair["old"]},
        )
    response = await review(client, pair)
    assert response.status_code == 409, response.text
    rows, conflict = await state(pair)
    assert conflict["review_status"] == "pending" and rows[pair["old"]]["status"] == status


async def test_a_changed_chain_is_not_overwritten_and_the_decision_stays_pending(client, pair):
    third = await memory(client, pair["tenant"])
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET supersedes_id = :third WHERE id = :id"),
            {"third": third, "id": pair["new"]},
        )
    response = await review(client, pair)
    assert response.status_code == 409, response.text
    rows, conflict = await state(pair)
    assert rows[pair["new"]]["supersedes_id"] == third and conflict["review_status"] == "pending"


async def test_failure_between_pair_updates_rolls_back_the_pair_and_decision(client, pair):
    writes = 0

    def fail_second_update(conn, cursor, statement, parameters, context, executemany):
        nonlocal writes
        if statement.lstrip().upper().startswith("UPDATE MEMORIES "):
            writes += 1
            if writes == 2:
                raise RuntimeError("synthetic second write failure")

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", fail_second_update)
    try:
        with pytest.raises(RuntimeError, match="synthetic second write failure"):
            await review(client, pair)
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_update)
    rows, conflict = await state(pair)
    assert conflict["review_status"] == "pending"
    assert rows[pair["old"]]["status"] == rows[pair["new"]]["status"] == "active"
    assert rows[pair["new"]]["supersedes_id"] is None


async def test_two_reviewers_cannot_apply_twice(client, pair):
    responses = await asyncio.gather(review(client, pair), review(client, pair, "replace"))
    assert sorted(r.status_code for r in responses) == [200, 409]
    rows, conflict = await state(pair)
    assert rows[pair["new"]]["supersedes_id"] == pair["old"]
    assert rows[pair["old"]]["status"] == (
        "outdated" if conflict["resolution_action"] == "supersede" else "conflicted"
    )


async def test_mark_disputed_marks_both_propositions_and_their_evidence(client, pair):
    response = await review(client, pair, "mark_disputed")
    assert response.status_code == 200, response.text
    rows, _ = await state(pair)
    for mid in (pair["old"], pair["new"]):
        assert rows[mid]["status"] == "conflicted" and rows[mid]["verification_status"] == "disputed"


async def test_no_op_reverses_only_the_pair_owned_detector_effect(client, pair):
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET supersedes_id = :old WHERE id = :new"),
            {"old": pair["old"], "new": pair["new"]},
        )
        await session.execute(
            text("UPDATE memories SET status = 'outdated' WHERE id = :old"), {"old": pair["old"]}
        )
        await session.execute(
            text("UPDATE memory_conflicts SET metadata = metadata || CAST(:effect AS jsonb) WHERE id = :id"),
            {"id": pair["conflict"], "effect": '{"effect_applied":true}'},
        )
    response = await review(client, pair, "no_op")
    assert response.status_code == 200, response.text
    rows, conflict = await state(pair)
    assert rows[pair["new"]]["supersedes_id"] is None
    assert rows[pair["old"]]["status"] == "active"
    assert conflict["metadata"]["review_effect_applied"] is False
    assert conflict["metadata"]["effect_applied"] is True


async def test_direction_uses_chronology_when_the_trigger_was_the_older_memory(client, pair):
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET created_at = '2026-09-01T00:00:00Z' WHERE id = :id"),
            {"id": pair["new"]},
        )
    response = await review(client, pair)
    assert response.status_code == 200, response.text
    rows, conflict = await state(pair)
    assert rows[pair["old"]]["supersedes_id"] == pair["new"]
    assert rows[pair["new"]]["status"] == "outdated"
    assert conflict["metadata"]["review_loser_id"] == pair["new"]


async def test_foreign_pair_member_is_not_read_or_mutated_by_review(client, pair):
    foreign = await memory(client, "foreign-" + uuid4().hex)
    async with get_session() as session:
        await session.execute(
            text("UPDATE memory_conflicts SET old_memory_id = :foreign WHERE id = :id"),
            {"foreign": foreign, "id": pair["conflict"]},
        )
    response = await review(client, pair)
    assert response.status_code == 409
    _, conflict = await state(pair)
    assert conflict["review_status"] == "pending"


async def test_other_successors_prevent_pair_restoration(client, pair):
    third = await memory(client, pair["tenant"])
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET supersedes_id = :old WHERE id = :id"), {"old": pair["old"], "id": third}
        )
    response = await review(client, pair, "no_op")
    assert response.status_code == 409
    rows, conflict = await state(pair)
    assert rows[third]["supersedes_id"] == pair["old"] and conflict["review_status"] == "pending"
