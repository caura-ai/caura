"""Correction propagation, CP-10: who to tell about a correction.

The ``caura.memory.corrected`` consumer (``common.events.correction_handlers``)
records a ``memory_correction`` whose recipients are the agents that read the
memory before it was corrected, the correcting agent excluded. The routes
``GET /corrections/{id}`` and ``GET /memories/{id}/corrections`` read it back,
tenant-scoped.

The integration test drives the whole chain on an in-process bus: two agents
read a memory, a trust-3 agent that also read it edits it, and the record
lists the two readers and not the editor.

See ``docs/plans/correction-propagation.md``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from common.events import correction_handlers
from common.events.base import Event
from common.events.memory_corrected import CorrectionActor, MemoryCorrected
from common.events.topics import Topics
from core_api.services.correction_recipients import redact_for_caller
from tests.conftest import get_admin_headers, uid

_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


class _FakeAdapter(correction_handlers.CorrectionStorageAdapter):
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def record_correction(self, *, tenant_id: str, body: dict) -> dict:
        self.calls.append((tenant_id, body))
        if self.fail:
            raise RuntimeError("storage down")
        return {"id": "c1", "created": True, "recipients": 2, "truncated": 0}


def _event(**over) -> Event:
    payload = MemoryCorrected(
        memory_id=uuid4(),
        tenant_id="t1",
        actor=CorrectionActor(kind="agent", id="editor"),
        kind="content_edit",
        corrected_at="2026-11-05T10:00:00+00:00",  # type: ignore[arg-type]
        old_summary="Tuesday",
        new_summary="Thursday",
    ).model_dump(mode="json")
    return Event(
        event_type=Topics.Memory.CORRECTED,
        tenant_id="t1",
        payload={**payload, **over},
    )


# ── unit: the consumer ─────────────────────────────────────────────────────


@pytest.mark.unit
async def test_the_event_becomes_one_storage_body() -> None:
    adapter = _FakeAdapter()
    event = _event()
    await correction_handlers._handle_memory_corrected(event, adapter=adapter)
    [(tenant, body)] = adapter.calls
    assert tenant == "t1"
    assert body == {
        "event_id": str(event.event_id),
        "memory_id": event.payload["memory_id"],
        "kind": "content_edit",
        "actor_kind": "agent",
        "actor_id": "editor",
        "corrected_at": "2026-11-05T10:00:00+00:00",
        "old_summary": "Tuesday",
        "new_summary": "Thursday",
        "detail": {},
    }


@pytest.mark.unit
async def test_a_malformed_payload_is_dropped() -> None:
    adapter = _FakeAdapter()
    await correction_handlers._handle_memory_corrected(
        _event(kind="auto_supersede"), adapter=adapter
    )
    assert adapter.calls == []


@pytest.mark.unit
async def test_a_storage_failure_raises_so_the_bus_redelivers() -> None:
    with pytest.raises(RuntimeError):
        await correction_handlers._handle_memory_corrected(
            _event(), adapter=_FakeAdapter(fail=True)
        )


@pytest.mark.unit
def test_core_worker_subscribes_in_the_manifest() -> None:
    import scripts.gen_events_manifest as gen

    manifest = gen.build_manifest()
    assert "caura.memory.corrected" in manifest["services"]["core-worker"]
    assert "caura.memory.corrected" not in manifest["services"]["core-api"]


@pytest.mark.unit
def test_old_summary_is_for_a_person_or_a_recipient_only() -> None:
    c = {"old_summary": "Tuesday", "recipients": [{"agent_id": "reader-a"}]}
    assert (
        redact_for_caller(c, agent_id=None, is_person=False)["old_summary"] == "Tuesday"
    )
    assert (
        redact_for_caller(c, agent_id=None, is_person=True)["old_summary"] == "Tuesday"
    )
    assert (
        redact_for_caller(c, agent_id="reader-a", is_person=False)["old_summary"]
        == "Tuesday"
    )
    assert (
        redact_for_caller(c, agent_id="stranger", is_person=False)["old_summary"]
        is None
    )


# ── integration: edit a memory two agents read ─────────────────────────────


@pytest.fixture
def as_agent():
    """Authenticate the REST client as an agent, as the gateway's X-Agent-ID does."""
    from core_api.app import app
    from core_api.audit_actor import audit_principal
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None) -> None:
        if agent_id is None:
            app.dependency_overrides.pop(get_auth_context, None)
            return

        async def _dep():
            set_current_tenant(tenant_id)
            # What get_auth_context records for an agent key: the correction
            # hook announces nothing without a request principal.
            audit_principal.set(
                {"user_id": None, "agent_id": agent_id, "credential_kind": "agent_key"}
            )
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _settle() -> None:
    from core_api.tasks import _background_tasks

    for _ in range(3):
        await asyncio.sleep(0)
        pending = [t for t in _background_tasks if not t.done()]
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=10
            )


@pytest.fixture
def bus(monkeypatch):
    """A real in-process bus carrying the publisher's event to the consumer."""
    from common.events import memory_corrected_publisher
    from common.events.inprocess import InProcessEventBus
    from core_api.services.correction_recipients import make_correction_adapter

    b = InProcessEventBus()
    monkeypatch.setattr(memory_corrected_publisher, "get_event_bus", lambda: b)
    monkeypatch.setattr(correction_handlers, "get_event_bus", lambda: b)
    correction_handlers.register_correction_consumer(make_correction_adapter())
    return b


@pytest.mark.integration
async def test_an_edit_lists_the_readers_and_not_the_editor(
    client, tenant_id, as_agent, bus, monkeypatch
) -> None:
    from core_api.services import agent_service

    real_lookup = agent_service.lookup_agent

    async def lookup(tid, agent_id, *, read=True):
        if agent_id == "editor":
            return {"agent_id": "editor", "trust_level": 3}
        return await real_lookup(tid, agent_id, read=read)

    monkeypatch.setattr(agent_service, "lookup_agent", lookup)

    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": f"The {uid()} deploy window is Tuesday." + _PADDING,
        },
        headers=get_admin_headers(),
    )
    assert w.status_code in (200, 201), w.text
    memory_id = w.json()["id"]

    for agent in ("reader-a", "reader-b", "editor"):
        as_agent(tenant_id, agent)
        g = await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}")
        assert g.status_code == 200, g.text
        await _settle()

    as_agent(tenant_id, "editor")
    r = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"content": "The deploy window moved to Thursday." + _PADDING},
    )
    assert r.status_code == 200, r.text
    await _settle()
    await bus.drain()
    await _settle()

    as_agent(tenant_id, None)
    lst = await client.get(
        f"/api/v1/memories/{memory_id}/corrections?tenant_id={tenant_id}",
        headers=get_admin_headers(),
    )
    assert lst.status_code == 200, lst.text
    [correction] = lst.json()["corrections"]
    assert correction["kind"] == "content_edit"
    assert correction["actor"] == {"kind": "agent", "id": "editor"}
    assert correction["status"] == "preview"
    assert {x["agent_id"] for x in correction["recipients"]} == {"reader-a", "reader-b"}
    assert correction["counts"]["recipients"] == 2
    assert correction["old_summary"].startswith("The ")

    one = await client.get(
        f"/api/v1/corrections/{correction['id']}?tenant_id={tenant_id}",
        headers=get_admin_headers(),
    )
    assert one.status_code == 200, one.text
    assert one.json()["id"] == correction["id"]

    # An agent that never read the memory does not see what it said.
    as_agent(tenant_id, "stranger")
    seen = await client.get(
        f"/api/v1/corrections/{correction['id']}?tenant_id={tenant_id}"
    )
    assert seen.status_code == 200, seen.text
    assert seen.json()["old_summary"] is None

    # Another tenant: 404 on both routes.
    other = f"{tenant_id}-other"
    as_agent(other, "outsider")
    assert (
        await client.get(f"/api/v1/corrections/{correction['id']}?tenant_id={other}")
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/memories/{memory_id}/corrections?tenant_id={other}")
    ).status_code == 404
