"""A PATCH that loses a race with a DELETE answers 404, not 500.

``update_memory`` checks that the row exists, writes, then re-reads it to build
the response. A DELETE can land in either gap. Before the write, storage finds
no live row and the client returns ``None``; after it, the re-read does. The
function handed that ``None`` to ``_dict_to_memory_out``, which raised
``AttributeError`` and surfaced as a 500. The loadtest's PATCH-versus-DELETE
probe hit it on the 2026-11-04 and 2026-11-05 staging nightlies. Either way
the memory is gone, which is the existence check's own answer: 404.

A DELETE before the write also means nothing changed, so nothing is audited
or reported as a correction.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _RacedClient:
    """Storage where a DELETE lands before the write or right after it."""

    def __init__(self, mem: dict, *, deleted: str):
        self._mem = mem
        self._deleted = deleted
        self.reads = 0
        self.writes: list[dict] = []

    async def get_memory(self, memory_id, tenant_id, *, read=True):
        self.reads += 1
        # The first read is the existence check; the DELETE comes after it,
        # so every later read finds nothing.
        return dict(self._mem) if self.reads == 1 else None

    async def update_memory(self, memory_id, tenant_id, patch):
        self.writes.append(patch)
        # ``_patch`` returns None for storage's 404: no live row to update.
        return None if self._deleted == "before_write" else {"ok": True}

    async def find_duplicate_hash(self, *a, **k):
        return None

    async def reset_entity_artifacts(self, tenant_id, memory_id):
        return {"links": 0, "relations": 0, "entities": 0}

    async def get_entity_links_for_memories(self, ids, tenant_id):
        return {}


class _Hooks:
    def __init__(self):
        self.audited: list[str] = []

    async def audit_log(self, *, action, **kw):
        self.audited.append(action)


async def _patch(monkeypatch, *, deleted: str, fields: dict):
    from core_api.schemas import MemoryUpdate
    from core_api.services import memory_service

    memory_id = uuid4()
    mem = {
        "id": str(memory_id),
        "tenant_id": "t1",
        "fleet_id": None,
        "agent_id": "a1",
        "content": "the original body of this memory",
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "subject_entity_id": None,
        "metadata_": {},
        "weight": 0.5,
        "recall_count": 0,
        "created_at": "2026-11-05T00:00:00+00:00",
        "updated_at": "2026-11-05T00:00:00+00:00",
    }
    client = _RacedClient(mem, deleted=deleted)
    hooks = _Hooks()
    corrections: list[str] = []
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: client)
    monkeypatch.setattr(memory_service, "get_hooks", lambda: hooks)
    monkeypatch.setattr(
        memory_service, "on_memory_corrected", lambda **k: corrections.append(k["kind"])
    )

    async def _embed(*a, **k):
        return [0.1, 0.2]

    monkeypatch.setattr(memory_service, "get_embedding", _embed)

    class _Config:
        semantic_dedup_enabled = False
        entity_extraction_enabled = False
        enrichment_enabled = False
        enrichment_provider = "none"

    async def _resolve_config(_tenant):
        return _Config()

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(memory_service, "track_task", close_scheduled_coro)
    monkeypatch.setattr(memory_service, "tracked_task", lambda coro, *a, **k: coro)

    with pytest.raises(HTTPException) as caught:
        await memory_service.update_memory(memory_id, "t1", MemoryUpdate(**fields))
    return caught.value, client, hooks, corrections


_EDITS = [
    pytest.param({"title": "a new title"}, id="title"),
    pytest.param({"content": "a replacement body for this memory"}, id="content"),
]


@pytest.mark.parametrize("fields", _EDITS)
async def test_a_delete_before_the_write_is_a_404_with_nothing_recorded(
    monkeypatch, fields
):
    err, client, hooks, corrections = await _patch(
        monkeypatch, deleted="before_write", fields=fields
    )
    assert (err.status_code, err.detail) == (404, "Memory not found")
    assert len(client.writes) == 1
    # Storage changed nothing, so neither the audit log nor the correction
    # events may say it did.
    assert hooks.audited == []
    assert corrections == []


@pytest.mark.parametrize("fields", _EDITS)
async def test_a_delete_after_the_write_is_a_404(monkeypatch, fields):
    err, client, hooks, _ = await _patch(
        monkeypatch, deleted="after_write", fields=fields
    )
    assert (err.status_code, err.detail) == (404, "Memory not found")
    assert len(client.writes) == 1
    # The write landed before the DELETE, so its audit entry stands.
    assert hooks.audited == ["update"]
