"""09/22 M-01 — every forward chain-edge write must REQUEST the storage CAS.

Six comments in ``contradiction_detector`` justified omitting guards by
citing a storage-side compare-and-set spelled ``WHERE supersedes_id IS
NULL``. That clause existed, but only inside ``PATCH
/memories/{id}/status``. All three forward paths (Path A RDF, Path A
semantic, Path C entity-overlap) flush through ``POST
/memories/batch-update-status``, which set the pointer unconditionally
via ``memory_update_status``. Its only CAS is the opt-in
``expected_supersedes_id`` ("only update if the pointer currently equals
X"), which cannot express "expect NULL" — ``None`` there means *no gate*
— and which no forward path passes.

The guard that was really missing is in the FLIPPED branch. It writes an
edge onto a PRE-EXISTING candidate row, gated only by
``newer.get("supersedes_id")`` read off a snapshot fetched before an LLM
judge that runs for seconds. A concurrent detection wiring an edge onto
the same candidate inside that window leaves the snapshot reading a
stale NULL; the flush then re-points the row and orphans whatever it had
just been made to supersede — the same orphan shape #1690 fixed from the
canonical side.

The fix adds ``expect_supersedes_null`` to the batch route (backed by
``memory_set_supersedes_if_null``, which both routes now share) and has
every forward edge write pass it. These tests pin the payload: an edge
write that does not carry the flag is an edge write with no backstop,
whatever the comment above it claims.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Row factories — a single-value-predicate triple so the RDF gate fires.
# ---------------------------------------------------------------------------

_SUBJECT = "00000000-0000-0000-0000-0000000000aa"


def _new_memory(*, mid, ts: str, value: str) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {value}",
        "subject_entity_id": _SUBJECT,
        "predicate": "lives_in",
        "object_value": value,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": ts,
    }


def _candidate(*, cid, ts: str, value: str, rdf: bool = True) -> dict:
    row = {
        "id": str(cid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {value}",
        "status": "active",
        "visibility": "scope_team",
        "object_value": value,
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": ts,
    }
    if not rdf:
        # Semantic / Path C candidates must NOT satisfy the triple gate,
        # or the deterministic pass short-circuits the loop under test.
        row["subject_entity_id"] = None
    return row


def _edge_rows(sc: AsyncMock) -> dict[str, dict]:
    """The flushed payload rows that actually carry a chain edge."""
    assert sc.batch_update_status.await_count == 1, (
        f"expected one flush, got {sc.batch_update_status.await_count}"
    )
    payload = sc.batch_update_status.call_args.args[0]
    return {r["memory_id"]: r for r in payload["updates"] if r.get("supersedes_id")}


def _assert_every_edge_is_cas_guarded(edges: dict[str, dict], *, label: str) -> None:
    assert edges, f"{label}: no chain edge was written at all"
    for mid, row in edges.items():
        assert row.get("expect_supersedes_null") is True, (
            f"{label}: edge write for {mid} points at {row['supersedes_id']} "
            f"with no CAS requested — a concurrent writer's edge would be "
            f"silently overwritten and its target orphaned. row={row}"
        )


# ---------------------------------------------------------------------------
# Path A — deterministic RDF pass
# ---------------------------------------------------------------------------


async def _run_path_a(new_mem: dict, candidates: list[dict], *, rdf: bool) -> AsyncMock:
    from core_api.services.contradiction_detector import _detect

    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=candidates if rdf else [])
    sc.find_similar_candidates = AsyncMock(return_value=[] if rdf else candidates)
    sc.batch_update_status = AsyncMock(
        return_value={"ok": True, "skipped": [], "edge_skipped": []}
    )

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            AsyncMock(return_value=(True, 0.95)),
        ),
    ):
        await _detect(new_mem, [0.1] * VECTOR_DIM)
    return sc


@pytest.mark.asyncio
async def test_rdf_canonical_edge_requests_the_cas():
    """Canonical: ``new_memory`` points at the older candidate."""
    older = _candidate(cid=uuid4(), ts="2026-04-29T10:00:00+00:00", value="Tel Aviv")
    new = _new_memory(mid=uuid4(), ts="2026-04-29T12:00:00+00:00", value="Haifa")

    sc = await _run_path_a(new, [older], rdf=True)
    edges = _edge_rows(sc)

    assert edges[new["id"]]["supersedes_id"] == older["id"]
    _assert_every_edge_is_cas_guarded(edges, label="rdf/canonical")


@pytest.mark.asyncio
async def test_rdf_flipped_edge_requests_the_cas():
    """Flipped: the edge lands on a PRE-EXISTING row, which is the case the
    application-level snapshot guard cannot hold on its own."""
    newer = _candidate(cid=uuid4(), ts="2026-04-29T14:00:00+00:00", value="Tel Aviv")
    new = _new_memory(mid=uuid4(), ts="2026-04-29T12:00:00+00:00", value="Haifa")

    sc = await _run_path_a(new, [newer], rdf=True)
    edges = _edge_rows(sc)

    assert edges[newer["id"]]["supersedes_id"] == new["id"], (
        "expected the flipped edge candidate → new_memory"
    )
    _assert_every_edge_is_cas_guarded(edges, label="rdf/flipped")


@pytest.mark.asyncio
async def test_rdf_mixed_run_guards_both_edges():
    """A mixed run writes two edges through ``_merge_status_update``. The
    merge must not drop the flag off either of them."""
    newer = _candidate(cid=uuid4(), ts="2026-04-29T14:00:00+00:00", value="Eilat")
    older = _candidate(cid=uuid4(), ts="2026-04-29T08:00:00+00:00", value="Tel Aviv")
    new = _new_memory(mid=uuid4(), ts="2026-04-29T12:00:00+00:00", value="Haifa")

    sc = await _run_path_a(new, [newer, older], rdf=True)
    edges = _edge_rows(sc)

    assert len(edges) == 2, f"expected a three-way chain, got {edges}"
    _assert_every_edge_is_cas_guarded(edges, label="rdf/mixed")


# ---------------------------------------------------------------------------
# Path A — semantic pass (the widest race window: a batch LLM judge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_canonical_edge_requests_the_cas():
    older = _candidate(
        cid=uuid4(), ts="2026-04-29T10:00:00+00:00", value="Tel Aviv", rdf=False
    )
    new = _new_memory(mid=uuid4(), ts="2026-04-29T12:00:00+00:00", value="Haifa")
    new["subject_entity_id"] = None  # force the semantic path

    sc = await _run_path_a(new, [older], rdf=False)
    edges = _edge_rows(sc)

    assert edges[new["id"]]["supersedes_id"] == older["id"]
    _assert_every_edge_is_cas_guarded(edges, label="semantic/canonical")


@pytest.mark.asyncio
async def test_semantic_flipped_edge_requests_the_cas():
    newer = _candidate(
        cid=uuid4(), ts="2026-04-29T14:00:00+00:00", value="Tel Aviv", rdf=False
    )
    new = _new_memory(mid=uuid4(), ts="2026-04-29T12:00:00+00:00", value="Haifa")
    new["subject_entity_id"] = None

    sc = await _run_path_a(new, [newer], rdf=False)
    edges = _edge_rows(sc)

    assert edges[newer["id"]]["supersedes_id"] == new["id"]
    _assert_every_edge_is_cas_guarded(edges, label="semantic/flipped")


# ---------------------------------------------------------------------------
# Path C — entity-overlap loop
# ---------------------------------------------------------------------------

_CONFLICT_RAW = {
    "subject_a": "X",
    "subject_b": "X",
    "same_subject": True,
    "non_conflict_reason": "none",
    "contradicts": True,
    "reason": "mutually exclusive states",
}


async def _run_path_c(new_mem: dict, candidates: list[dict]) -> AsyncMock:
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    sc = AsyncMock()

    async def get_memory(mid: str, tenant_id: str, **_kw):
        for row in (new_mem, *candidates):
            if row["id"] == mid:
                return row
        return None

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=candidates)
    sc.get_entity_links_for_memories = AsyncMock(return_value={})
    sc.batch_update_status = AsyncMock(
        return_value={"ok": True, "skipped": [], "edge_skipped": []}
    )

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check_batch",
            AsyncMock(side_effect=lambda _c, cands, _cfg: [_CONFLICT_RAW] * len(cands)),
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            AsyncMock(return_value=(True, 0.95)),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_entity_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_mem["id"], "t1", "f1")
    return sc


def _path_c_row(mid, *, ts: str, content: str) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": ts,
    }


@pytest.mark.asyncio
async def test_path_c_mixed_run_guards_both_edges():
    """Path C's three-way chain — both the canonical and the flipped edge."""
    new_id = str(uuid4())
    newer = _path_c_row(uuid4(), ts="2026-05-24T12:00:00+00:00", content="X in Newer")
    older = _path_c_row(uuid4(), ts="2026-05-24T08:00:00+00:00", content="X in Older")
    new_mem = _path_c_row(new_id, ts="2026-05-24T10:00:00+00:00", content="X in Haifa")

    sc = await _run_path_c(new_mem, [newer, older])
    edges = _edge_rows(sc)

    assert edges[newer["id"]]["supersedes_id"] == new_id
    assert edges[new_id]["supersedes_id"] == older["id"]
    _assert_every_edge_is_cas_guarded(edges, label="path_c/mixed")


# ---------------------------------------------------------------------------
# The claim itself — assert the storage side really offers what the
# comments name, so this cannot silently become phantom again.
# ---------------------------------------------------------------------------


def test_storage_exposes_a_named_null_cas():
    """``memory_set_supersedes_if_null`` is the clause the detector's
    comments have always invoked. It must exist and be the ONE thing both
    status routes use — the phantom survived because the clause lived
    inline in one route and nowhere else."""
    import inspect

    from core_storage_api.routers import memories as routes
    from core_storage_api.services.postgres_service import PostgresService

    assert hasattr(PostgresService, "memory_set_supersedes_if_null")

    src = inspect.getsource(PostgresService.memory_set_supersedes_if_null)
    assert "supersedes_id.is_(None)" in src, (
        "the NULL CAS no longer gates on a NULL pointer"
    )

    route_src = inspect.getsource(routes)
    assert route_src.count("memory_set_supersedes_if_null") >= 2, (
        "both the single-row and the batch status route must go through the "
        "shared CAS; an inlined copy is how 09/22 M-01 happened"
    )
