"""Audit P5 — collapse N+1 storage HTTPs in the crystallizer archive sweep.

Two loops in ``_run_crystallization`` previously fired:
  - per-id ``sc.get_memory(mid)`` across the full candidate set
  - per-cluster-member ``sc.update_memory_status(mid, "archived")``

After the fix:
  - one ``sc.bulk_get_memories`` call across the full candidate set
  - one ``sc.batch_update_status`` per cluster (K HTTPs replacing K x M)

These shape-explicit tests assert the post-fix wire effect directly. The
LLM crystallize step + the memory-create step are mocked out so the
test focuses on the archive sweep without LLM / DB side effects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.services.crystallizer_service import _run_crystallization

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _pair(a: str, b: str) -> dict:
    """Near-duplicate pair entry matching ``_build_clusters`` input shape."""
    return {"id1": a, "id2": b}


def _memory_row(mid: UUID) -> dict:
    """Minimal memory dict the cluster body reads via ``.get()``."""
    return {
        "id": str(mid),
        "content": f"content for {mid}",
        "memory_type": "fact",
        "status": "active",
    }


async def _stub_resolve_config(_db, _tenant_id):
    """Bypass DB lookup — return an object with the attributes
    ``_run_crystallization`` reads from ``config``. Only
    ``_crystallize_cluster`` actually touches it; that call is mocked
    out by these tests, so a placeholder is enough."""
    from types import SimpleNamespace

    return SimpleNamespace()


def _build_storage_mock(memories: list[dict]) -> AsyncMock:
    """Return a storage-client mock recording bulk-vs-per-row archive calls."""
    sc = AsyncMock()
    # ``bulk_get_memories`` returns rows aligned to input ids; the
    # crystallizer zip+if-not-None filter handles None slots.
    sc.bulk_get_memories = AsyncMock(return_value=memories)
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    # Per-row archive should never be called; spy on it to catch a
    # regression that re-introduces the N+1 shape.
    sc.update_memory_status = AsyncMock()
    sc.get_memory = AsyncMock()
    return sc


async def test_archive_sweep_uses_one_bulk_get_for_all_candidates():
    """``bulk_get_memories`` replaces the per-id ``get_memory`` loop —
    the storage client sees one call with all ``total_ids`` in input
    order."""
    a, b, c = uuid4(), uuid4(), uuid4()
    pairs = [_pair(str(a), str(b)), _pair(str(b), str(c)), _pair(str(a), str(c))]
    hygiene = {"near_duplicates": {"pairs": pairs}}
    memories = [_memory_row(a), _memory_row(b), _memory_row(c)]
    sc = _build_storage_mock(memories)

    # Drive ``_crystallize_cluster`` to return no extracted facts so the
    # cluster processing loop continues to the archive sweep without
    # exercising ``create_memory`` (which would require a real db).
    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            _stub_resolve_config,
        ),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            # Return at least one extracted fact so cluster processing
            # reaches the archive sweep (empty list triggers
            # if not extracted: continue and skips the cluster).
            AsyncMock(
                return_value=[
                    {"content": "crystallized", "memory_type": "fact", "weight": 0.8}
                ]
            ),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            # Stand-in for the new-memory create — the crystallizer
            # imports it locally, so patch the source module.
            AsyncMock(return_value=type("_MemOut", (), {"id": uuid4()})()),
        ),
    ):
        await _run_crystallization(
            db=None, tenant_id="t1", fleet_id=None, hygiene=hygiene
        )

    # Exactly one bulk-get; per-row get_memory never fired.
    assert sc.bulk_get_memories.call_count == 1, (
        f"expected 1 bulk_get_memories, got {sc.bulk_get_memories.call_count}"
    )
    assert sc.get_memory.call_count == 0, (
        f"per-row get_memory must be 0 after P5; got {sc.get_memory.call_count}"
    )
    requested_ids = sc.bulk_get_memories.call_args.args[0]
    assert set(requested_ids) == {str(a), str(b), str(c)}


async def test_archive_sweep_collapses_to_one_batch_per_cluster():
    """Cluster of 3 → one ``batch_update_status`` call with 3 updates;
    zero per-row ``update_memory_status`` calls."""
    a, b, c = uuid4(), uuid4(), uuid4()
    pairs = [_pair(str(a), str(b)), _pair(str(b), str(c)), _pair(str(a), str(c))]
    hygiene = {"near_duplicates": {"pairs": pairs}}
    memories = [_memory_row(a), _memory_row(b), _memory_row(c)]
    sc = _build_storage_mock(memories)

    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            _stub_resolve_config,
        ),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            # Return at least one extracted fact so cluster processing
            # reaches the archive sweep (empty list triggers
            # if not extracted: continue and skips the cluster).
            AsyncMock(
                return_value=[
                    {"content": "crystallized", "memory_type": "fact", "weight": 0.8}
                ]
            ),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            # Stand-in for the new-memory create — the crystallizer
            # imports it locally, so patch the source module.
            AsyncMock(return_value=type("_MemOut", (), {"id": uuid4()})()),
        ),
    ):
        result = await _run_crystallization(
            db=None, tenant_id="t1", fleet_id=None, hygiene=hygiene
        )

    assert sc.update_memory_status.call_count == 0, (
        f"per-row update_memory_status must be 0 after P5; "
        f"got {sc.update_memory_status.call_count}"
    )
    assert sc.batch_update_status.call_count == 1, (
        f"expected 1 batch_update_status for the single cluster; "
        f"got {sc.batch_update_status.call_count}"
    )
    payload = sc.batch_update_status.call_args.args[0]
    assert len(payload["updates"]) == 3
    assert all(u["status"] == "archived" for u in payload["updates"])
    assert {u["memory_id"] for u in payload["updates"]} == {str(a), str(b), str(c)}
    # All three archive writes counted in the result.
    assert result["memories_archived"] == 3


async def test_archive_sweep_drops_skipped_rows_from_archived_count():
    """Storage reports one row in ``skipped`` (CAS miss / deleted) —
    the cluster's archive count must reflect only the rows that
    actually landed, matching the prior per-row try/except shape that
    dropped failing ids."""
    a, b, c = uuid4(), uuid4(), uuid4()
    pairs = [_pair(str(a), str(b)), _pair(str(b), str(c)), _pair(str(a), str(c))]
    hygiene = {"near_duplicates": {"pairs": pairs}}
    memories = [_memory_row(a), _memory_row(b), _memory_row(c)]

    sc = AsyncMock()
    sc.bulk_get_memories = AsyncMock(return_value=memories)
    # ``c`` reported back as skipped — the count must drop to 2.
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": [str(c)]})
    sc.update_memory_status = AsyncMock()
    sc.get_memory = AsyncMock()

    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            _stub_resolve_config,
        ),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            # Return at least one extracted fact so cluster processing
            # reaches the archive sweep (empty list triggers
            # if not extracted: continue and skips the cluster).
            AsyncMock(
                return_value=[
                    {"content": "crystallized", "memory_type": "fact", "weight": 0.8}
                ]
            ),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            # Stand-in for the new-memory create — the crystallizer
            # imports it locally, so patch the source module.
            AsyncMock(return_value=type("_MemOut", (), {"id": uuid4()})()),
        ),
    ):
        result = await _run_crystallization(
            db=None, tenant_id="t1", fleet_id=None, hygiene=hygiene
        )

    assert result["memories_archived"] == 2  # ``c`` was skipped


async def test_archive_sweep_skips_bulk_get_when_no_candidates():
    """No near-duplicate pairs → no candidates → ``bulk_get_memories``
    is never called (avoid an empty round-trip)."""
    hygiene = {"near_duplicates": {"pairs": []}}
    sc = _build_storage_mock([])

    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            _stub_resolve_config,
        ),
    ):
        result = await _run_crystallization(
            db=None, tenant_id="t1", fleet_id=None, hygiene=hygiene
        )

    assert sc.bulk_get_memories.call_count == 0
    assert sc.batch_update_status.call_count == 0
    assert result["memories_archived"] == 0


async def test_archive_sweep_handles_none_slots_for_deleted_memories():
    """``bulk_get_memories`` returns ``None`` in slots for ids that no
    longer exist (or were soft-deleted between dedup-check and archive
    sweep). Those ids drop out of the cluster — same as the old loop's
    ``if mem:`` skip. If the surviving cluster falls below
    ``MIN_CLUSTER_SIZE`` the cluster gets skipped entirely."""
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    pairs = [
        _pair(str(a), str(b)),
        _pair(str(b), str(c)),
        _pair(str(c), str(d)),
    ]
    hygiene = {"near_duplicates": {"pairs": pairs}}
    # 4-member cluster; ``b`` deleted between detect and sweep.
    memories = [_memory_row(a), None, _memory_row(c), _memory_row(d)]
    sc = _build_storage_mock(memories)

    with (
        patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            _stub_resolve_config,
        ),
        patch(
            "core_api.services.crystallizer_service._crystallize_cluster",
            # Return at least one extracted fact so cluster processing
            # reaches the archive sweep (empty list triggers
            # if not extracted: continue and skips the cluster).
            AsyncMock(
                return_value=[
                    {"content": "crystallized", "memory_type": "fact", "weight": 0.8}
                ]
            ),
        ),
        patch(
            "core_api.services.memory_service.create_memory",
            # Stand-in for the new-memory create — the crystallizer
            # imports it locally, so patch the source module.
            AsyncMock(return_value=type("_MemOut", (), {"id": uuid4()})()),
        ),
    ):
        result = await _run_crystallization(
            db=None, tenant_id="t1", fleet_id=None, hygiene=hygiene
        )

    # Cluster shrank from 4 → 3 after dropping the None slot; still
    # meets MIN_CLUSTER_SIZE so the batch fires with the 3 survivors.
    assert sc.batch_update_status.call_count == 1
    archived_ids = {
        u["memory_id"] for u in sc.batch_update_status.call_args.args[0]["updates"]
    }
    assert archived_ids == {str(a), str(c), str(d)}
    assert str(b) not in archived_ids
    assert result["memories_archived"] == 3
