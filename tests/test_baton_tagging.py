"""Handoff batons, D5: the write path links a receiver's writes to its baton.

HB-14 measured 9 of 25 receiver writes carrying ``metadata.baton_id`` when the
model had to pass it. Now a write is tagged with the baton waiting on the
writer, and an explicit ``baton_id`` is kept only from that baton's sender or
a receiver. ``services/baton_tagging.py`` has the rules; storage's half is
``core-storage-api/tests/test_baton_notices.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from core_api.audit_actor import audit_principal
from core_api.config import settings
from core_api.services import baton_tagging as bt

B1 = "00000001-0000-4000-8000-000000000000"
B2 = "00000002-0000-4000-8000-000000000000"


def _row(baton_id: str, message_id: str, **extra) -> dict:
    return {
        "baton_id": baton_id,
        "from_agent": "sender",
        "goal": "Finish the csv-diff edge cases",
        "created_at": "2026-11-05T09:00:00+00:00",
        "message_id": message_id,
        "bus_checked_at": None,
        **extra,
    }


@pytest.fixture
def storage(monkeypatch):
    sc = MagicMock(name="storage_client")
    sc.list_baton_notices = AsyncMock(return_value=[])
    sc.list_baton_recipients = AsyncMock(return_value=[])
    sc.accept_baton = AsyncMock(return_value=None)
    sc.mark_baton_notices_checked = AsyncMock(return_value=0)
    monkeypatch.setattr(bt, "get_storage_client", lambda: sc)
    monkeypatch.setattr(settings, "collaboration_api_url", "")
    return sc


@pytest.fixture
def bus_states(monkeypatch):
    """The bus's state per baton for the receiver; every check is recorded."""
    states: dict[str, str] = {}
    asked: list[str] = []

    async def _bus_state(_tenant, _agent, baton_id):
        asked.append(baton_id)
        return states.get(baton_id, "delivered")

    monkeypatch.setattr(settings, "collaboration_api_url", "http://bus.test")
    monkeypatch.setattr(bt, "_bus_state", _bus_state)
    return states, asked


@pytest.mark.unit
async def test_a_write_from_a_receiver_is_tagged_with_its_waiting_baton(storage):
    storage.list_baton_notices.return_value = [_row(B1, "msg-1")]

    out = await bt.tag_write("t-1", "engine", {"topic": "csv"})

    storage.list_baton_notices.assert_awaited_once_with(
        "t-1", "engine", limit=bt.MAX_OPEN
    )
    assert out == {"topic": "csv", "baton_id": B1, "_system": {"baton_tagged": "auto"}}


@pytest.mark.unit
async def test_no_metadata_is_tagged_too(storage):
    storage.list_baton_notices.return_value = [_row(B1, "msg-1")]
    assert (await bt.tag_write("t-1", "engine", None))["baton_id"] == B1


@pytest.mark.unit
async def test_nothing_waiting_leaves_the_write_as_it_is(storage):
    meta = {"topic": "csv"}
    assert await bt.tag_write("t-1", "engine", meta) is meta
    assert await bt.tag_write("t-1", "engine", None) is None


@pytest.mark.unit
async def test_the_newest_waiting_baton_wins(storage):
    storage.list_baton_notices.return_value = [_row(B2, "msg-2"), _row(B1, "msg-1")]
    assert (await bt.tag_write("t-1", "engine", {}))["baton_id"] == B2


@pytest.mark.unit
async def test_bus_provenance_names_its_own_message(storage):
    storage.list_baton_notices.return_value = [_row(B2, "msg-2"), _row(B1, "msg-1")]
    bus = {"source": "caura-bus", "thread_id": "t", "message_id": "msg-1"}
    assert (await bt.tag_write("t-1", "engine", bus))["baton_id"] == B1
    # Another thread's context (HB-14: contract writes for the baton's work) is
    # tagged like a plain write.
    other = {"source": "caura-bus", "thread_id": "t", "message_id": "msg-9"}
    assert (await bt.tag_write("t-1", "engine", other))["baton_id"] == B2


@pytest.mark.unit
async def test_a_person_or_no_agent_is_never_tagged(storage):
    storage.list_baton_notices.return_value = [_row(B1, "msg-1")]
    assert await bt.tag_write("t-1", None, {}) == {}
    token = audit_principal.set({"credential_kind": "person"})
    try:
        assert await bt.tag_write("t-1", "engine", {}) == {}
    finally:
        audit_principal.reset(token)
    storage.list_baton_notices.assert_not_awaited()


@pytest.mark.unit
async def test_a_failed_lookup_is_no_tag(storage):
    storage.list_baton_notices.side_effect = RuntimeError("storage down")
    assert await bt.tag_write("t-1", "engine", {"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# The bus owns delivery state: a baton already answered there is not tagged
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_baton_replied_on_the_bus_is_copied_back_and_skipped(
    storage, bus_states
):
    states, asked = bus_states
    states[B2] = "replied"
    storage.list_baton_notices.return_value = [_row(B2, "msg-2"), _row(B1, "msg-1")]

    out = await bt.tag_write("t-1", "engine", {})

    assert out["baton_id"] == B1 and asked == [B2, B1]
    storage.accept_baton.assert_awaited_once_with(
        "t-1", B2, agent_id="engine", via="bus", state="replied"
    )


@pytest.mark.unit
async def test_the_bus_is_read_on_every_write_even_just_after_a_check(
    storage, bus_states
):
    # A receiver that replied a moment ago must not tag its next write with it.
    states, asked = bus_states
    now = datetime.now(UTC).isoformat()
    storage.list_baton_notices.return_value = [_row(B1, "msg-1", bus_checked_at=now)]
    states[B1] = "replied"
    assert "baton_id" not in await bt.tag_write("t-1", "engine", {})
    assert asked == [B1]


@pytest.mark.unit
async def test_an_unreachable_bus_keeps_the_table_state(storage, monkeypatch):
    monkeypatch.setattr(settings, "collaboration_api_url", "http://bus.test")
    monkeypatch.setattr(
        bt, "_bus_state", AsyncMock(side_effect=RuntimeError("bus down"))
    )
    storage.list_baton_notices.return_value = [_row(B1, "msg-1")]
    assert (await bt.tag_write("t-1", "engine", {}))["baton_id"] == B1


# ---------------------------------------------------------------------------
# An explicit baton_id: only from that baton's sender or a receiver
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("writer", ["engine", "sender"])
async def test_a_party_keeps_its_explicit_baton_id(storage, writer):
    storage.list_baton_recipients.return_value = [
        {"agent_id": "engine", "from_agent": "sender"}
    ]
    out = await bt.tag_write("t-1", writer, {"baton_id": B1.upper()})
    storage.list_baton_recipients.assert_awaited_once_with("t-1", B1)
    assert out == {"baton_id": B1}
    storage.list_baton_notices.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("baton_id", [B1, "not-a-uuid", ""])
async def test_anyone_else_is_refused(storage, baton_id):
    storage.list_baton_recipients.return_value = [
        {"agent_id": "engine", "from_agent": "sender"}
    ]
    with pytest.raises(HTTPException) as err:
        await bt.tag_write("t-1", "outsider", {"baton_id": baton_id})
    assert err.value.status_code == 422


@pytest.mark.unit
async def test_an_unknown_baton_is_refused(storage):
    with pytest.raises(HTTPException):
        await bt.tag_write("t-1", "engine", {"baton_id": B1})


# ---------------------------------------------------------------------------
# Wiring: every agent write surface goes through it, platform writes do not
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_create_memory_tags_and_platform_writes_do_not(monkeypatch):
    from core_api.schemas import MemoryCreate
    from core_api.services import memory_service

    seen: list = []

    async def tag(tenant_id, agent_id, metadata, sc=None):
        seen.append((tenant_id, agent_id))
        return {**(metadata or {}), "baton_id": B1}

    written: list = []

    async def pipeline(data, *, is_inferred=False):
        written.append(data.metadata)
        return MagicMock()

    monkeypatch.setattr(bt, "tag_write", tag)
    monkeypatch.setattr(memory_service, "_run_write_pipeline", pipeline)
    monkeypatch.setattr(
        "core_api.services.value_ledger.schedule_origin", lambda *a, **k: None
    )
    data = MemoryCreate(
        tenant_id="t-1", agent_id="engine", content="found the bug", metadata={"x": 1}
    )
    await memory_service.create_memory(data)
    assert seen == [("t-1", "engine")] and written[-1]["baton_id"] == B1

    plain = MemoryCreate(tenant_id="t-1", agent_id="engine", content="an insight")
    await memory_service.create_memory(plain, is_inferred=True)
    assert len(seen) == 1 and "baton_id" not in (written[-1] or {})


@pytest.mark.unit
async def test_a_bulk_write_reads_the_waiting_batons_once(storage):
    storage.list_baton_notices.return_value = [_row(B2, "msg-2"), _row(B1, "msg-1")]
    tagger = bt.BatonTagger("t-1", "engine", storage)
    out = [
        await tagger.tag(m)
        for m in ({}, None, {"source": "caura-bus", "message_id": "msg-1"})
    ]
    assert [m["baton_id"] for m in out] == [B2, B2, B1]
    storage.list_baton_notices.assert_awaited_once()
