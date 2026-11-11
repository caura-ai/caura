"""Correction propagation CP-08/CP-09: the ``caura.memory.corrected`` event.

One hook (``core_api.services.memory_corrections``) announces a correction made
by a person, a tenant or admin key, or an agent at ``trust_level >= 3``:

- a content edit (``PATCH /memories/{id}``);
- a status change to ``outdated``/``cancelled``/``deleted``, including a delete;
- withdrawing a ``human_verified`` (unverify);
- a conflict review.

It writes an audit entry ``memory.corrected`` without the memory's text and
publishes the event with the summaries, after the write and off the request
path. The contradiction detector's automatic supersede announces nothing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from common.events.memory_corrected import SUMMARY_MAX_CHARS, MemoryCorrected, summarize
from common.events.topics import Topics
from core_api.audit_actor import audit_principal
from core_api.services import memory_corrections
from tests._contradiction_batch_compat import install_batch_status_replay_shim
from tests.conftest import get_admin_headers, uid

_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)

_PERSON = {
    "user_id": "user-7",
    "surface": "dashboard",
    "agent_id": None,
    "credential_kind": "person",
}


async def _settle() -> None:
    """Await the hook's background tasks. The suite's own drain cancels them."""
    from core_api.tasks import _background_tasks

    for _ in range(3):
        await asyncio.sleep(0)
        mine = [
            t
            for t in _background_tasks
            if not t.done()
            and t.get_coro().__name__ in ("_emit", "_conflict_corrected")
        ]
        if mine:
            await asyncio.wait_for(asyncio.gather(*mine), timeout=10)


@pytest.fixture
def captured(monkeypatch) -> tuple[list[MemoryCorrected], list[dict]]:
    """The events published and the ``memory.corrected`` audit rows written."""
    events: list[MemoryCorrected] = []
    audits: list[dict] = []

    async def fake_publish(payload: MemoryCorrected) -> None:
        events.append(payload)

    from core_api.services import audit_service

    real_log_action = audit_service.log_action

    async def spy_log_action(**kwargs):
        if kwargs.get("action") == memory_corrections.MEMORY_CORRECTED_ACTION:
            audits.append(kwargs)
        await real_log_action(**kwargs)

    monkeypatch.setattr(memory_corrections, "publish_memory_corrected", fake_publish)
    monkeypatch.setattr(audit_service, "log_action", spy_log_action)
    return events, audits


@pytest.fixture
def as_principal():
    """Set the request principal. Called inside the test, so it lands in the
    test's own context and goes with it."""
    return audit_principal.set


def _trust(monkeypatch, level: int | None) -> None:
    from core_api.services import agent_service

    async def fake_lookup(tenant_id, agent_id, *, read=True):
        return None if level is None else {"agent_id": agent_id, "trust_level": level}

    monkeypatch.setattr(agent_service, "lookup_agent", fake_lookup)


def _memory(**fields) -> dict:
    return {
        "id": str(uuid4()),
        "fleet_id": "fleet-a",
        "content": "old text",
        "status": "active",
        **fields,
    }


# ── unit: the payload ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_topic_is_declared() -> None:
    assert Topics.Memory.CORRECTED == "caura.memory.corrected"


@pytest.mark.unit
def test_summaries_are_cut_to_the_limit() -> None:
    assert summarize(None) is None
    assert summarize("short") == "short"
    long = summarize("x" * 1000)
    assert long is not None and len(long) == SUMMARY_MAX_CHARS and long.endswith("…")


# ── unit: who counts (D2) ──────────────────────────────────────────────────


@pytest.mark.unit
async def test_a_person_content_edit_publishes_with_summaries(
    captured, as_principal
) -> None:
    events, audits = captured
    as_principal(_PERSON)
    mem = _memory(content="Alice lives in Tel Aviv. " * 30)
    memory_corrections.on_memory_corrected(
        tenant_id="t1",
        memory=mem,
        kind="content_edit",
        agent_id=None,
        old_summary=mem["content"],
        new_summary="Alice lives in Haifa.",
    )
    await _settle()
    [event] = events
    assert str(event.memory_id) == mem["id"]
    assert (event.tenant_id, event.fleet_id, event.kind) == (
        "t1",
        "fleet-a",
        "content_edit",
    )
    assert (event.actor.kind, event.actor.id) == ("user", "user-7")
    assert event.old_summary is not None and len(event.old_summary) == SUMMARY_MAX_CHARS
    assert event.new_summary == "Alice lives in Haifa."
    [audit] = audits
    assert audit["resource_id"] == event.memory_id
    # The text rides on the event only.
    assert "Tel Aviv" not in repr(audit) and "Haifa" not in repr(audit)


@pytest.mark.unit
async def test_no_request_principal_announces_nothing(captured, as_principal) -> None:
    """A background job (detector, merge, lifecycle) has no principal."""
    events, audits = captured
    as_principal(None)
    memory_corrections.on_status_corrected(
        tenant_id="t1",
        memory=_memory(),
        old_status="active",
        new_status="outdated",
        agent_id=None,
    )
    await _settle()
    assert events == [] and audits == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("trust", "fires"), [(None, False), (1, False), (2, False), (3, True), (4, True)]
)
async def test_an_agent_corrects_only_at_trust_3(
    captured, as_principal, monkeypatch, trust, fires
) -> None:
    events, _ = captured
    as_principal({**_PERSON, "user_id": None, "credential_kind": "agent_key"})
    _trust(monkeypatch, trust)
    memory_corrections.on_status_corrected(
        tenant_id="t1",
        memory=_memory(),
        old_status="active",
        new_status="cancelled",
        agent_id="bot",
    )
    await _settle()
    assert bool(events) is fires
    if fires:
        assert (events[0].actor.kind, events[0].actor.id) == ("agent", "bot")


@pytest.mark.unit
async def test_an_install_credential_is_not_a_person(captured, as_principal) -> None:
    events, _ = captured
    as_principal({**_PERSON, "user_id": None, "credential_kind": "install_credential"})
    memory_corrections.on_status_corrected(
        tenant_id="t1",
        memory=_memory(),
        old_status="active",
        new_status="deleted",
        agent_id=None,
    )
    await _settle()
    assert events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old", "new"),
    [("active", "confirmed"), ("active", "archived"), ("outdated", "outdated")],
)
async def test_other_status_changes_are_not_corrections(
    captured, as_principal, old, new
) -> None:
    events, _ = captured
    as_principal(_PERSON)
    memory_corrections.on_status_corrected(
        tenant_id="t1", memory=_memory(), old_status=old, new_status=new, agent_id=None
    )
    await _settle()
    assert events == []


@pytest.mark.unit
async def test_a_publish_failure_never_raises(monkeypatch, as_principal) -> None:
    as_principal(_PERSON)
    monkeypatch.setattr(
        memory_corrections,
        "publish_memory_corrected",
        AsyncMock(side_effect=RuntimeError("bus down")),
    )
    memory_corrections.on_status_corrected(
        tenant_id="t1",
        memory=_memory(),
        old_status="active",
        new_status="outdated",
        agent_id=None,
    )
    await _settle()


@pytest.mark.unit
async def test_a_conflict_review_names_the_old_memory(captured, as_principal) -> None:
    events, audits = captured
    as_principal(_PERSON)
    old, new = _memory(content="Bob is CTO"), _memory(content="Bob is CEO")
    rows = {old["id"]: old, new["id"]: new}
    sc = AsyncMock()
    sc.get_memory = AsyncMock(side_effect=lambda mid, tenant, read=True: rows[mid])
    with patch("core_api.clients.storage_client.get_storage_client", return_value=sc):
        memory_corrections.on_conflict_resolved(
            tenant_id="t1",
            conflict={
                "id": "c-1",
                "old_memory_id": old["id"],
                "new_memory_id": new["id"],
            },
            review_status="resolved",
            resolution_action=None,
            agent_id=None,
        )
        await _settle()
    [event] = events
    assert str(event.memory_id) == old["id"]
    assert (event.kind, event.old_summary, event.new_summary) == (
        "conflict_resolve",
        "Bob is CTO",
        "Bob is CEO",
    )
    assert event.detail["related_memory_id"] == new["id"]
    assert "CTO" not in repr(audits)


# ── negative: the contradiction detector's automatic supersede ─────────────


@pytest.mark.unit
async def test_detector_supersede_announces_nothing(captured, as_principal) -> None:
    """Detection runs in a task spawned by a person's write, so it even inherits
    that request's principal. It retires rows through storage directly and never
    reaches the hook."""
    from core_api.services.contradiction_detector import _detect

    events, audits = captured
    as_principal(_PERSON)
    old_id, new_id = str(uuid4()), str(uuid4())
    old_memory = {
        "id": old_id,
        "tenant_id": "t1",
        "content": "Alice lives in Tel Aviv",
        "status": "active",
        "object_value": "Tel Aviv",
        "created_at": "2026-04-29T11:00:00+00:00",
    }
    new_memory = {
        "id": new_id,
        "tenant_id": "t1",
        "fleet_id": None,
        "subject_entity_id": str(uuid4()),
        "predicate": "lives_in",
        "object_value": "Haifa",
        "content": "Alice lives in Haifa",
        "supersedes_id": None,
        "status": "active",
        "created_at": "2026-04-29T12:00:00+00:00",
    }
    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_memory])
    mock_sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(mock_sc)
    with patch(
        "core_api.services.contradiction_detector.get_storage_client",
        return_value=mock_sc,
    ):
        contradictions = await _detect(new_memory, [0.1] * 10)
    await _settle()
    # The detector did supersede ...
    assert len(contradictions) == 1
    mock_sc.update_memory_status.assert_any_call(old_id, "outdated")
    # ... and nothing was announced.
    assert events == [] and audits == []


# ── integration: each hook path through the real routes ──────────────────


async def _write(client, tenant_id: str) -> dict:
    r = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "fleet_id": "fleet-cp",
            "memory_type": "fact",
            "content": f"The release train leaves on Tuesday {uid()}." + _PADDING,
        },
        headers=get_admin_headers(),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.mark.integration
async def test_content_edit_route(client, tenant_id, captured) -> None:
    events, audits = captured
    out = await _write(client, tenant_id)
    new_text = f"The release train leaves on Thursday {uid()}." + _PADDING
    r = await client.patch(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}",
        json={"content": new_text},
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    await _settle()
    [event] = events
    assert (str(event.memory_id), event.kind, event.fleet_id) == (
        out["id"],
        "content_edit",
        "fleet-cp",
    )
    assert event.actor.kind == "admin_key"
    assert event.old_summary is not None and event.old_summary.startswith(
        "The release train leaves on Tuesday"
    )
    assert event.new_summary is not None and event.new_summary.startswith(
        "The release train leaves on Thursday"
    )
    [audit] = audits
    assert "Tuesday" not in repr(audit) and "Thursday" not in repr(audit)


@pytest.mark.integration
async def test_an_edit_that_changes_no_text_is_not_a_correction(
    client, tenant_id, captured
) -> None:
    events, _ = captured
    out = await _write(client, tenant_id)
    r = await client.patch(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}",
        json={"weight": 0.9},
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    await _settle()
    assert events == []


@pytest.mark.integration
async def test_status_route(client, tenant_id, captured) -> None:
    events, audits = captured
    out = await _write(client, tenant_id)
    r = await client.patch(
        f"/api/v1/memories/{out['id']}/status?tenant_id={tenant_id}",
        json={"status": "outdated"},
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    await _settle()
    [event] = events
    assert (event.kind, event.old_summary, event.new_summary) == (
        "status",
        "active",
        "outdated",
    )
    assert event.detail == {"old_status": "active", "new_status": "outdated"}
    [audit] = audits
    assert (audit["detail"]["old_status"], audit["detail"]["new_status"]) == (
        "active",
        "outdated",
    )


@pytest.mark.integration
async def test_delete_route(client, tenant_id, captured) -> None:
    events, _ = captured
    out = await _write(client, tenant_id)
    r = await client.delete(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}",
        headers=get_admin_headers(),
    )
    assert r.status_code in (200, 204), r.text
    await _settle()
    [event] = events
    assert (event.kind, event.new_summary) == ("status", "deleted")


@pytest.mark.integration
async def test_unverify_route(client, tenant_id, captured) -> None:
    events, _ = captured
    out = await _write(client, tenant_id)
    base = f"/api/v1/memories/{out['id']}"
    assert (
        await client.post(
            f"{base}/verify?tenant_id={tenant_id}", headers=get_admin_headers()
        )
    ).status_code == 200
    await _settle()
    assert events == []  # verifying corrects nothing
    r = await client.post(
        f"{base}/unverify?tenant_id={tenant_id}", headers=get_admin_headers()
    )
    assert r.status_code == 200, r.text
    await _settle()
    [event] = events
    assert (event.kind, event.old_summary, event.new_summary) == (
        "unverify",
        "human_verified",
        "unverified",
    )
    # A second unverify has nothing left to withdraw.
    await client.post(
        f"{base}/unverify?tenant_id={tenant_id}", headers=get_admin_headers()
    )
    await _settle()
    assert len(events) == 1


@pytest.mark.integration
async def test_the_event_reaches_a_bus_subscriber(
    client, tenant_id, monkeypatch
) -> None:
    """The real publisher on an in-process bus, not a stub."""
    from common.events import memory_corrected_publisher
    from common.events.base import Event
    from common.events.inprocess import InProcessEventBus

    bus = InProcessEventBus()
    monkeypatch.setattr(memory_corrected_publisher, "get_event_bus", lambda: bus)
    got: list[Event] = []

    async def handler(event: Event) -> None:
        got.append(event)

    bus.subscribe(Topics.Memory.CORRECTED, handler)
    out = await _write(client, tenant_id)
    r = await client.patch(
        f"/api/v1/memories/{out['id']}/status?tenant_id={tenant_id}",
        json={"status": "cancelled"},
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    await _settle()
    await bus.drain()
    [event] = got
    assert event.payload["memory_id"] == out["id"]
    assert event.event_type == "caura.memory.corrected"
    assert event.tenant_id == tenant_id
    assert MemoryCorrected(**event.payload).kind == "status"
