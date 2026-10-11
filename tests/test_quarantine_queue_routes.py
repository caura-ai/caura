"""Core-api's half of the write gate's review queue and session rollback (g2.9).

A signed-in person lists the held memories (g2.7) a page at a time and can undo
what one broker session wrote. Both are a person's: an agent or an API key gets
a 403 and storage is never asked. A rollback audits every memory it changed, on
that memory, with the person who did it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api import errors
from core_api.auth import AuthContext
from core_api.pagination import decode_cursor
from core_api.routes import memories as memories_routes
from core_api.schemas import SessionRollbackRequest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CLIENT = "core_api.clients.storage_client.CoreStorageClient"
_PERSON = AuthContext(
    tenant_id="t", org_role="admin", is_person=True, user_id="user-7", surface="prism"
)
_NOT_PEOPLE = {
    "agent": AuthContext(tenant_id="t", agent_id="a1"),
    "tenant key": AuthContext(tenant_id="t"),
}
_T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def _held(minute: int) -> dict:
    """A held memory as storage's queue returns one."""
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": "t",
        "fleet_id": "f",
        "agent_id": "a1",
        "memory_type": "fact",
        "content": f"held write {minute}",
        "weight": 0.5,
        "metadata": {"session_id": "s-1"},
        "created_at": (_T0 + timedelta(minutes=minute)).isoformat(),
        "status": QUARANTINED_MEMORY_STATUS,
        "visibility": "scope_team",
        "recall_count": 0,
    }


@pytest.fixture
def storage(monkeypatch):
    calls = {
        "list": AsyncMock(),
        "rollback": AsyncMock(),
        "audit": AsyncMock(),
    }
    monkeypatch.setattr(f"{_CLIENT}.list_held_memories", calls["list"])
    monkeypatch.setattr(f"{_CLIENT}.rollback_session", calls["rollback"])
    monkeypatch.setattr(memories_routes, "log_action", calls["audit"])
    return calls


async def _list(auth: AuthContext, **params):
    query = {"tenant_id": "t", "session_id": None, "limit": 2, "cursor": None, **params}
    return await memories_routes.list_held_memories(**query, auth=auth)


async def _rollback(auth: AuthContext, session_id: str = "s-1"):
    return await memories_routes.rollback_session(
        SessionRollbackRequest(session_id=session_id), tenant_id="t", auth=auth
    )


# ── The queue ──


async def test_a_person_reads_the_queue_a_page_at_a_time(storage):
    rows = [_held(3), _held(2), _held(1)]  # one past the page: there is a next one
    storage["list"].return_value = {"items": rows, "total": 7}

    page = await _list(_PERSON, session_id="s-1")

    assert [str(item.id) for item in page.items] == [rows[0]["id"], rows[1]["id"]]
    assert {item.status for item in page.items} == {QUARANTINED_MEMORY_STATUS}
    assert page.total == 7
    assert decode_cursor(page.next_cursor) == (
        datetime.fromisoformat(rows[1]["created_at"]),
        uuid.UUID(rows[1]["id"]),
    )
    asked = storage["list"].await_args
    assert asked.args == ("t",)
    assert asked.kwargs == {
        "session_id": "s-1",
        "limit": 3,
        "cursor_ts": None,
        "cursor_id": None,
    }


async def test_the_last_page_has_no_cursor_and_a_cursor_is_passed_on(storage):
    storage["list"].return_value = {"items": [_held(2), _held(1), _held(0)], "total": 3}
    cursor = (await _list(_PERSON)).next_cursor
    storage["list"].return_value = {"items": [_held(0)], "total": 3}

    last = await _list(_PERSON, cursor=cursor)

    assert last.next_cursor is None
    asked = storage["list"].await_args.kwargs
    assert (asked["cursor_ts"], asked["cursor_id"]) == decode_cursor(cursor)


async def test_a_cursor_that_is_not_one_is_a_400(storage):
    with pytest.raises(HTTPException) as refused:
        await _list(_PERSON, cursor="not-a-cursor")
    assert refused.value.status_code == 400
    storage["list"].assert_not_awaited()


@pytest.mark.parametrize("who", sorted(_NOT_PEOPLE))
async def test_only_a_person_reads_the_queue(storage, who):
    with pytest.raises(HTTPException) as refused:
        await _list(_NOT_PEOPLE[who])
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == errors.AUTH_PERSON_REQUIRED
    storage["list"].assert_not_awaited()


@pytest.mark.parametrize("who", sorted(_NOT_PEOPLE))
async def test_only_a_person_rolls_a_session_back(storage, who):
    with pytest.raises(HTTPException) as refused:
        await _rollback(_NOT_PEOPLE[who])
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == errors.AUTH_PERSON_REQUIRED
    storage["rollback"].assert_not_awaited()


# ── Session rollback ──


async def test_a_person_rolls_a_session_back_and_each_memory_is_audited(storage):
    storage["rollback"].return_value = {
        "outdated": ["m-live", "m-child"],
        "restored": ["m-superseded"],
        "cancelled": ["m-held"],
    }

    result = await _rollback(_PERSON)

    assert storage["rollback"].await_args.args == ("t", "s-1")
    assert (result.session_id, result.outdated, result.restored, result.cancelled) == (
        "s-1",
        ["m-live", "m-child"],
        ["m-superseded"],
        ["m-held"],
    )
    audited = [call.kwargs for call in storage["audit"].await_args_list]
    assert [(row["resource_id"], row["detail"]["new_status"]) for row in audited] == [
        ("m-live", "outdated"),
        ("m-child", "outdated"),
        ("m-superseded", "active"),
        ("m-held", "cancelled"),
    ]
    for row in audited:
        assert row["action"] == "session.rollback"
        assert row["resource_type"] == "memory"
        assert row["tenant_id"] == "t"
        assert row["detail"]["session_id"] == "s-1"
        assert (row["detail"]["user_id"], row["detail"]["surface"]) == (
            "user-7",
            "prism",
        )
        # A full audit queue writes it to storage rather than dropping it.
        assert row["critical"] is True


async def test_a_failed_audit_write_costs_only_its_own_row(storage, caplog):
    """The rollback is committed and a retry changes nothing, so a failed write
    must not stop the rows after it, nor turn the response into an error."""
    storage["rollback"].return_value = {
        "outdated": ["m-1", "m-2"],
        "restored": [],
        "cancelled": ["m-3"],
    }
    storage["audit"].side_effect = [RuntimeError("storage down"), None, None]

    with caplog.at_level(logging.ERROR, logger=memories_routes.__name__):
        result = await _rollback(_PERSON)

    assert (result.outdated, result.cancelled) == (["m-1", "m-2"], ["m-3"])
    audited = [call.kwargs["resource_id"] for call in storage["audit"].await_args_list]
    assert audited == ["m-1", "m-2", "m-3"]
    assert "m-1" in caplog.text


async def test_a_rollback_with_nothing_left_audits_nothing(storage):
    storage["rollback"].return_value = {"outdated": [], "restored": [], "cancelled": []}

    result = await _rollback(_PERSON)

    assert (result.outdated, result.restored, result.cancelled) == ([], [], [])
    storage["audit"].assert_not_awaited()


@pytest.mark.parametrize(
    "auth",
    [
        # A person on a read-only credential.
        AuthContext(
            tenant_id="t", org_role="admin", is_person=True, capabilities={"read"}
        ),
        # A person asking for another tenant.
        AuthContext(tenant_id="other", org_role="admin", is_person=True),
    ],
)
async def test_a_person_needs_write_and_the_tenant_to_roll_back(storage, auth):
    with pytest.raises(HTTPException) as refused:
        await _rollback(auth)
    assert refused.value.status_code == 403
    storage["rollback"].assert_not_awaited()
    storage["audit"].assert_not_awaited()
