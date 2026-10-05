"""B25 (L-33, L-34): the inline enrichment write-back merges, and its fan-out
uses what it just wrote.

L-33: ``_enrich_memory_background`` read the row, copied its metadata, set the
enrichment keys on the copy and sent the whole dict back as ``metadata_``,
which storage assigns wholesale. Any metadata change committed between that
read and the write (a caller's PATCH, a concurrent platform marker) was
reverted. It now sends a ``metadata_patch``, which storage merges, clearing
``enrichment_pending`` as ``False`` in both homes as core-worker already does.

L-34: the atomic-fact children took ``ts_valid_start`` from that same pre-write
read, so a validity start the enrichment had just resolved for the parent never
reached them. They now take the patched value.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.memory_enrichment import AtomicFact

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-b25-inline"
VALID_FROM = "2026-03-01T00:00:00+00:00"


def _enrichment(**fields):
    return SimpleNamespace(
        **{
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "title": "t",
            "summary": "platform summary",
            "tags": [],
            "llm_ms": 1,
            "contains_pii": False,
            "pii_types": [],
            "business_relevance": "business",
            "retrieval_hint": "",
            "ts_valid_start": None,
            "ts_valid_end": None,
            "atomic_facts": [],
            **fields,
        }
    )


async def _run(enrichment, *, row_metadata=None):
    """Drive one inline enrichment; return ``(storage, children)``."""
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(
        return_value={
            "id": str(uuid.uuid4()),
            "memory_type": "fact",
            "status": "active",
            "weight": 0.5,
            "ts_valid_start": None,
            "ts_valid_end": None,
            "metadata_": row_metadata or {},
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": None,
            "content": "body",
            "visibility": "scope_team",
        }
    )
    sc.update_memory = AsyncMock(return_value=None)
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})

    def _stub_tracked_task(coro, *_a, **_k):
        coro.close()

    async def _embed(_content, tenant_config=None, **_kw):
        return [0.0]

    config = SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="fake",
        entity_extraction_enabled=False,
    )
    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", new=_stub_tracked_task),
        patch.object(memory_service, "get_embedding", new=_embed),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "body", TENANT, "f1", "a"
        )
    children = [c.args[0] for c in sc.create_memory.await_args_list]
    return sc, children


def _enrichment_patch(sc) -> dict:
    """The patch the enrichment write-back sent (the first ``update_memory``)."""
    assert sc.update_memory.await_count >= 1, "the enrichment write-back never ran"
    return sc.update_memory.await_args_list[0].args[2]


async def test_the_write_back_merges_rather_than_replaces_the_metadata():
    """L-33: no ``metadata_`` (assigned wholesale), only a ``metadata_patch``,
    and none of the snapshot's own keys are written back."""
    sc, _ = await _run(_enrichment(), row_metadata={"caller_note": "kept"})

    sent = _enrichment_patch(sc)
    assert "metadata_" not in sent, "the write-back still replaces the metadata"
    meta = sent["metadata_patch"]
    assert "caller_note" not in meta, "a key read from the snapshot was written back"
    assert meta["summary"] == "platform summary"
    assert meta["business_relevance"] == "business"
    # A merge cannot delete a key, so the pending flag is cleared as False, in
    # both homes, exactly as core-worker clears it.
    assert meta["enrichment_pending"] is False
    assert meta["_system"]["enrichment_pending"] is False


async def test_the_children_take_the_validity_start_the_enrichment_wrote():
    """L-34: the parent row read before the write had no ``ts_valid_start``."""
    facts = [AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")]
    sc, children = await _run(
        _enrichment(ts_valid_start=VALID_FROM, atomic_facts=facts)
    )

    assert _enrichment_patch(sc)["ts_valid_start"] == VALID_FROM
    assert len(children) == 2
    assert all(child["ts_valid_start"] == VALID_FROM for child in children)
