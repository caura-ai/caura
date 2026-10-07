"""Core-api's half of a held memory (``quarantined``).

A person reviewing a held memory may open it, release it (``active``) or reject
it (``cancelled``), and the audit row names that person. Nobody else sees it:
an agent or a machine key asking for it by id gets a 404, as on every read
storage serves. And nothing written for it reaches another memory before a
person releases it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.auth import AuthContext
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write import schedule_background_tasks as background
from core_api.routes import memories as memories_routes

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CLIENT = "core_api.clients.storage_client.CoreStorageClient"
_PERSON = AuthContext(
    tenant_id="t", org_role="admin", is_person=True, user_id="user-7", surface="prism"
)
_TENANT_KEY = AuthContext(tenant_id="t")
_AGENT = AuthContext(tenant_id="t", agent_id="a1")


def _memory(status: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": status,
        "agent_id": "a1",
        "fleet_id": None,
        "visibility": "scope_team",
    }


@pytest.fixture
def storage(monkeypatch):
    calls = {
        "get": AsyncMock(),
        "update": AsyncMock(return_value={"ok": True}),
        "audit": AsyncMock(),
    }
    monkeypatch.setattr(f"{_CLIENT}.get_memory", calls["get"])
    monkeypatch.setattr(f"{_CLIENT}.update_memory_status", calls["update"])
    monkeypatch.setattr(memories_routes, "log_action", calls["audit"])
    monkeypatch.setattr(
        memories_routes, "authorize_memory_access", AsyncMock(return_value=True)
    )
    return calls


async def _set_status(auth: AuthContext, status: str) -> dict:
    return await memories_routes.update_memory_status(
        uuid.uuid4(), {"status": status}, tenant_id="t", auth=auth
    )


@pytest.mark.parametrize(
    ("exit_status", "action"),
    [("active", "quarantine.release"), ("cancelled", "quarantine.reject")],
)
async def test_a_person_releases_or_rejects_a_held_memory(storage, exit_status, action):
    storage["get"].return_value = _memory(QUARANTINED_MEMORY_STATUS)

    result = await _set_status(_PERSON, exit_status)

    assert result["old_status"] == QUARANTINED_MEMORY_STATUS
    assert result["new_status"] == exit_status
    assert storage["get"].await_args.kwargs["include_held"] is True
    assert storage["update"].await_args.kwargs["release_hold"] is True
    audit = storage["audit"].await_args.kwargs
    assert audit["action"] == action
    assert audit["detail"] == {
        "old_status": QUARANTINED_MEMORY_STATUS,
        "new_status": exit_status,
        "owner_agent_id": "a1",
        "user_id": "user-7",
        "surface": "prism",
    }


@pytest.mark.parametrize(
    "status", ["outdated", "conflicted", "archived", "pending", "confirmed"]
)
async def test_a_held_memory_goes_nowhere_else(storage, status):
    storage["get"].return_value = _memory(QUARANTINED_MEMORY_STATUS)

    with pytest.raises(HTTPException) as refused:
        await _set_status(_PERSON, status)

    assert refused.value.status_code == 409
    storage["update"].assert_not_awaited()
    storage["audit"].assert_not_awaited()


async def test_a_second_reviewer_is_told_it_is_no_longer_held(storage):
    storage["get"].return_value = _memory(QUARANTINED_MEMORY_STATUS)
    storage["update"].return_value = None  # storage matched no held row

    with pytest.raises(HTTPException) as refused:
        await _set_status(_PERSON, "active")

    assert refused.value.status_code == 409
    storage["audit"].assert_not_awaited()


@pytest.mark.parametrize("auth", [_AGENT, _TENANT_KEY], ids=["agent", "machine key"])
async def test_only_a_person_is_shown_a_held_memory_to_move(storage, auth):
    storage["get"].return_value = None  # what storage answers without include_held

    with pytest.raises(HTTPException) as refused:
        await _set_status(auth, "active")

    assert refused.value.status_code == 404
    assert storage["get"].await_args.kwargs["include_held"] is False
    storage["update"].assert_not_awaited()


@pytest.mark.parametrize("auth", [_PERSON, _AGENT], ids=["person", "agent"])
async def test_an_ordinary_transition_is_unchanged(storage, auth):
    storage["get"].return_value = _memory("active")

    await _set_status(auth, "outdated")

    assert storage["update"].await_args.kwargs["release_hold"] is False
    audit = storage["audit"].await_args.kwargs
    assert audit["action"] == "status_update"
    assert audit["detail"] == {
        "old_status": "active",
        "new_status": "outdated",
        "owner_agent_id": "a1",
    }


@pytest.mark.parametrize(
    ("auth", "include_held"), [(_PERSON, True), (_AGENT, False), (_TENANT_KEY, False)]
)
async def test_a_read_by_id_asks_for_held_rows_for_a_person_only(
    monkeypatch, auth, include_held
):
    detail = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{_CLIENT}.get_memory_detail", detail)

    with pytest.raises(HTTPException) as missing:
        await memories_routes.get_memory(uuid.uuid4(), tenant_id="t", auth=auth)

    assert missing.value.status_code == 404
    assert detail.await_args.kwargs["include_held"] is include_held


# ── Nothing a held write schedules reaches another memory ──


async def test_the_merge_retires_nothing_when_the_new_row_was_not_linked():
    """Storage answers no row for a held memory (or one deleted since), so
    nothing stands in the candidate's place and it must stay current."""
    client = MagicMock()
    client.update_memory_status = AsyncMock(return_value=None)
    with patch.object(background, "get_storage_client", return_value=client):
        await background._merge_near_duplicate(
            str(uuid.uuid4()), str(uuid.uuid4()), "t"
        )

    client.update_memory_status.assert_awaited_once()


async def test_the_merge_still_retires_the_candidate_once_linked():
    client = MagicMock()
    client.update_memory_status = AsyncMock(return_value={"ok": True})
    candidate = str(uuid.uuid4())
    with patch.object(background, "get_storage_client", return_value=client):
        await background._merge_near_duplicate(str(uuid.uuid4()), candidate, "t")

    assert client.update_memory_status.await_args.args[:2] == (candidate, "outdated")


async def _scheduled(memory_status: str) -> list[str]:
    from core_api.schemas import MemoryCreate

    labels: list[str] = []

    def _tracked(coro, label, *args, **kwargs):
        labels.append(label)
        coro.close()
        return MagicMock()

    ctx = PipelineContext(
        data={
            "input": MemoryCreate(
                tenant_id="t", fleet_id="f1", agent_id="a1", content="x" * 40
            ),
            "memory": {"id": uuid.uuid4(), "status": memory_status, "metadata_": {}},
            "embedding": [0.1] * 1024,
            "enrichment": type("E", (), {"atomic_facts": [MagicMock()]})(),
        },
        tenant_config=type("C", (), {"entity_extraction_enabled": False})(),
    )
    with (
        patch.object(
            background, "track_task", new=MagicMock(side_effect=lambda task: task)
        ),
        patch.object(background, "tracked_task", new=MagicMock(side_effect=_tracked)),
    ):
        await background.ScheduleBackgroundTasks().execute(ctx)
    return labels


async def test_a_held_write_gets_no_children_until_released():
    """The atomic-fact children would be live rows carrying its claims."""
    assert "atomic_fact_fanout" in await _scheduled("active")
    assert "atomic_fact_fanout" not in await _scheduled(QUARANTINED_MEMORY_STATUS)


# ── A release replays what the held write skipped (g2.8) ──


@pytest.fixture
def replays(monkeypatch):
    found = {
        "replay": MagicMock(return_value="replay-coroutine"),
        "tracked": MagicMock(return_value="tracked-task"),
        "track": MagicMock(),
    }
    monkeypatch.setattr(memories_routes, "replay_released_write", found["replay"])
    monkeypatch.setattr(memories_routes, "tracked_task", found["tracked"])
    monkeypatch.setattr(memories_routes, "track_task", found["track"])
    return found


async def test_a_release_replays_what_the_held_write_skipped(storage, replays):
    storage["get"].return_value = _memory(QUARANTINED_MEMORY_STATUS)
    memory_id = uuid.uuid4()

    await memories_routes.update_memory_status(
        memory_id, {"status": "active"}, tenant_id="t", auth=_PERSON
    )

    replays["replay"].assert_called_once_with(str(memory_id), "t")
    assert replays["tracked"].call_args.args[:2] == (
        "replay-coroutine",
        "release_replay",
    )
    replays["track"].assert_called_once_with("tracked-task")


@pytest.mark.parametrize(
    ("before", "after"),
    [(QUARANTINED_MEMORY_STATUS, "cancelled"), ("active", "outdated")],
    ids=["reject", "ordinary transition"],
)
async def test_nothing_else_replays(storage, replays, before, after):
    storage["get"].return_value = _memory(before)

    await _set_status(_PERSON, after)

    replays["replay"].assert_not_called()
    replays["track"].assert_not_called()


async def test_a_held_writes_chunk_moves_only_with_it(storage, replays):
    """Its auto-chunks are released or rejected with the write, and the queue
    lists only the write."""
    chunk = _memory(QUARANTINED_MEMORY_STATUS)
    chunk["metadata_"] = {"parent_memory_id": str(uuid.uuid4()), "source": "auto_chunk"}
    storage["get"].return_value = chunk

    with pytest.raises(HTTPException) as refused:
        await _set_status(_PERSON, "active")

    assert refused.value.status_code == 409
    storage["update"].assert_not_awaited()
    replays["replay"].assert_not_called()
