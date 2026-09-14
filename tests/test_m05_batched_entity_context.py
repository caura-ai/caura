"""oss-0902-m-05 — Path C fetches entity context for every candidate in ONE
batched pair of storage calls, not one fan-out group per candidate.

The N+1 this pins, as it was before the fix:

  * ``_fetch_entity_context`` was called once per candidate, and each call
    issued ``get_entity_links_for_memories([ONE_id])`` — a batch endpoint,
    always able to take the whole list, invoked with a single-element one.
  * then one ``get_entity`` per link, per candidate, with no dedup across
    candidates — even though entity-overlap candidates are selected
    BECAUSE they share entity rows with the new memory, so the same entity
    was refetched once per candidate linking it.

A detection pass at the cap (40 candidates, ~3 links each) therefore made
~160 HTTP round-trips where two suffice. The admission gate (#1461) bounds
how many passes contend at once, so round-trips inside a held slot is
precisely the term that sets gated throughput — the gate queued this cost,
it did not remove it.

``test_detection_pass_makes_one_links_call_and_one_entity_call`` is the
round-trip pin and the test that fails without the fix (pre-fix it records
``get_entity_links_for_memories.call_count == 21``). The rest assert the
fix is behaviour-preserving: identical contexts, identical order, identical
missing-entity handling, and the per-memory path still reachable as a
fallback when a batch call fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit

TENANT = "t1"


# ---------------------------------------------------------------------------
# Fixtures — a storage double that records round-trips
# ---------------------------------------------------------------------------


def _entity_row(eid: str) -> dict:
    return {
        "id": eid,
        "canonical_name": f"name-{eid}",
        "entity_type": "person" if eid.endswith("0") else "project",
    }


def _install_entity_storage(
    sc: AsyncMock,
    links_by_mem: dict[str, list[dict]],
    entities: dict[str, dict] | None = None,
) -> None:
    """Wire the two entity reads onto ``sc`` with REAL return shapes.

    A bare ``AsyncMock`` attribute would answer ``get_entities_by_ids`` with
    a ``MagicMock``, which the detector deliberately treats as shape drift
    and falls back from — so a round-trip assertion against one would be
    measuring the fallback, not the batch.
    """
    entity_rows = entities if entities is not None else {}

    async def get_entity_links_for_memories(memory_ids, tenant_id):
        assert tenant_id == TENANT, f"links read lost the tenant: {tenant_id!r}"
        return {mid: links_by_mem.get(mid, []) for mid in memory_ids}

    async def get_entities_by_ids(entity_ids, tenant_id):
        assert tenant_id == TENANT, f"batch hydration lost the tenant: {tenant_id!r}"
        return {eid: entity_rows.get(eid, _entity_row(eid)) for eid in entity_ids}

    async def get_entity(entity_id, tenant_id):
        assert tenant_id == TENANT, f"entity hydration lost the tenant: {tenant_id!r}"
        return entity_rows.get(entity_id, _entity_row(entity_id))

    sc.get_entity_links_for_memories = AsyncMock(
        side_effect=get_entity_links_for_memories
    )
    sc.get_entities_by_ids = AsyncMock(side_effect=get_entities_by_ids)
    sc.get_entity = AsyncMock(side_effect=get_entity)


def _memory(mid, content: str, *, subject_entity_id=None) -> dict:
    return {
        "id": str(mid),
        "tenant_id": TENANT,
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": subject_entity_id,
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": "2026-09-14T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# The round-trip pin — fails pre-fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detection_pass_makes_one_links_call_and_one_entity_call():
    """20 candidates, one links call, one entity-hydration call, no per-id reads.

    Drives the real Path C entry point rather than the helper, because the
    N+1 lived at the call site (a per-candidate generator feeding
    ``_bounded_gather``), not inside the per-memory helper — which was
    correct in isolation the whole time.
    """
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    new_mem = _memory(new_id, "Priya lives in Tel Aviv.")
    candidates = [_memory(uuid4(), f"Priya lived in city {i}.") for i in range(20)]

    # Shared subject entity (the reason they are candidates at all) plus one
    # object entity each — the shape that made the pre-fix path refetch
    # ``ent-subject`` twenty-one times.
    links_by_mem = {
        new_mem["id"]: [
            {"entity_id": "ent-subject", "role": "subject"},
            {"entity_id": "ent-new-obj", "role": "object"},
        ]
    }
    for i, c in enumerate(candidates):
        links_by_mem[c["id"]] = [
            {"entity_id": "ent-subject", "role": "subject"},
            {"entity_id": f"ent-obj-{i}", "role": "object"},
        ]

    sc = AsyncMock()

    async def get_memory(mid, tenant_id, **_kw):
        if mid == new_mem["id"]:
            return new_mem
        return next((c for c in candidates if c["id"] == mid), None)

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=candidates)
    sc.update_memory_status = AsyncMock()
    _install_entity_storage(sc, links_by_mem)
    install_batch_status_replay_shim(sc)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.9),
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.9),
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
        await detect_contradictions_by_entities_async(new_id, TENANT, "f1")

    assert sc.get_entity_links_for_memories.call_count == 1, (
        "one batched links call for the whole candidate set; pre-fix this was "
        f"1 + len(candidates) = 21, observed {sc.get_entity_links_for_memories.call_count}"
    )
    assert sc.get_entities_by_ids.call_count == 1, (
        f"one batched hydration call; observed {sc.get_entities_by_ids.call_count}"
    )
    assert sc.get_entity.call_count == 0, (
        "per-id hydration is the fallback only; pre-fix this was ~42 calls "
        f"(2 per memory, no cross-candidate dedup), observed {sc.get_entity.call_count}"
    )

    # The batch asked for each entity ONCE even though 21 memories link
    # ``ent-subject`` — dedup is half the saving and is easy to lose.
    asked = sc.get_entities_by_ids.call_args.args[0]
    assert len(asked) == len(set(asked)), f"duplicate ids in the batch request: {asked}"
    assert asked.count("ent-subject") == 1


# ---------------------------------------------------------------------------
# Behaviour identity vs the per-memory helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_contexts_match_the_per_memory_helper_exactly():
    """Same rows, same order, for every memory — the judge must not notice."""
    from core_api.services.contradiction_detector import (
        _fetch_entity_context,
        _fetch_entity_contexts,
    )

    mem_a, mem_b, mem_c = str(uuid4()), str(uuid4()), str(uuid4())
    links_by_mem = {
        # Deliberate: the same entity twice under DIFFERENT roles must stay
        # two rows, and link order is what the prompt renders.
        mem_a: [
            {"entity_id": "e1", "role": "subject"},
            {"entity_id": "e2", "role": "object"},
            {"entity_id": "e1", "role": "mentioned"},
        ],
        mem_b: [{"entity_id": "e2", "role": "subject"}],
        mem_c: [],
    }
    entities = {
        "e1": {"canonical_name": "Priya", "entity_type": "person"},
        # ``name`` rather than ``canonical_name`` — the legacy-shape
        # tolerance the per-memory helper has always had.
        "e2": {"name": "Project Helios", "entity_type": "project"},
    }

    sc = AsyncMock()
    _install_entity_storage(sc, links_by_mem, entities)

    batched = await _fetch_entity_contexts(sc, [mem_a, mem_b, mem_c], TENANT)
    per_memory = {
        mid: await _fetch_entity_context(sc, mid, TENANT)
        for mid in (mem_a, mem_b, mem_c)
    }

    assert batched == per_memory
    assert [r["role"] for r in batched[mem_a]] == ["subject", "object", "mentioned"]
    assert [r["name"] for r in batched[mem_a]] == ["Priya", "Project Helios", "Priya"]
    assert batched[mem_c] == []


@pytest.mark.asyncio
async def test_missing_entity_drops_only_its_own_link():
    """An id absent from the batch answers like ``get_entity`` returning None."""
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem = str(uuid4())
    sc = AsyncMock()

    async def get_entity_links_for_memories(memory_ids, tenant_id):
        return {
            mem: [
                {"entity_id": "present", "role": "subject"},
                {"entity_id": "deleted", "role": "object"},
                {"entity_id": None, "role": "object"},
            ]
        }

    async def get_entities_by_ids(entity_ids, tenant_id):
        # ``deleted`` is simply absent — a soft-deleted row, or one this
        # tenant does not own. Storage filters rather than erroring.
        return {"present": {"canonical_name": "Kept", "entity_type": "person"}}

    sc.get_entity_links_for_memories = AsyncMock(
        side_effect=get_entity_links_for_memories
    )
    sc.get_entities_by_ids = AsyncMock(side_effect=get_entities_by_ids)

    out = await _fetch_entity_contexts(sc, [mem], TENANT)

    assert out[mem] == [
        {
            "name": "Kept",
            "entity_type": "person",
            "role": "subject",
            "entity_id": "present",
        }
    ]


@pytest.mark.asyncio
async def test_memory_with_no_links_gets_an_empty_list_not_a_missing_key():
    """Callers index the mapping directly; a KeyError here would be a 500 in
    a background task, and an absent key would read as "not fetched"."""
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem = str(uuid4())
    sc = AsyncMock()
    _install_entity_storage(sc, {mem: []})

    out = await _fetch_entity_contexts(sc, [mem], TENANT)

    assert out == {mem: []}
    sc.get_entities_by_ids.assert_not_called()


# ---------------------------------------------------------------------------
# Fallbacks — batching must not be a new way to lose context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_links_batch_failure_falls_back_to_per_memory_fetch():
    """A rejected 41-id body must degrade to the old shape, not to empty.

    One oversized payload is a failure mode single-id calls did not have,
    so the fallback is the difference between a slower run and a silently
    context-free one (which reverts Path C to the base judge).
    """
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem_a, mem_b = str(uuid4()), str(uuid4())
    links_by_mem = {
        mem_a: [{"entity_id": "e1", "role": "subject"}],
        mem_b: [{"entity_id": "e2", "role": "subject"}],
    }
    sc = AsyncMock()
    _install_entity_storage(sc, links_by_mem)

    batched_call = {"n": 0}
    per_memory_impl = sc.get_entity_links_for_memories.side_effect

    async def fail_on_batch(memory_ids, tenant_id):
        if len(memory_ids) > 1:
            batched_call["n"] += 1
            raise RuntimeError("413 Payload Too Large")
        return await per_memory_impl(memory_ids, tenant_id)

    sc.get_entity_links_for_memories = AsyncMock(side_effect=fail_on_batch)

    out = await _fetch_entity_contexts(sc, [mem_a, mem_b], TENANT)

    assert batched_call["n"] == 1, "the batch was attempted first"
    assert out[mem_a][0]["entity_id"] == "e1"
    assert out[mem_b][0]["entity_id"] == "e2"


@pytest.mark.asyncio
async def test_entity_batch_failure_falls_back_to_per_id_hydration():
    """A core-storage-api that does not serve ``/entities/by-ids`` yet.

    The two services deploy separately, so this window is real on every
    rollout, not hypothetical.
    """
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem = str(uuid4())
    sc = AsyncMock()
    _install_entity_storage(
        sc,
        {
            mem: [
                {"entity_id": "e1", "role": "subject"},
                {"entity_id": "e2", "role": "object"},
            ]
        },
        {"e1": {"canonical_name": "Priya", "entity_type": "person"}},
    )
    sc.get_entities_by_ids = AsyncMock(side_effect=RuntimeError("404 Not Found"))

    out = await _fetch_entity_contexts(sc, [mem], TENANT)

    assert [r["name"] for r in out[mem]] == ["Priya", "name-e2"]
    assert sc.get_entity.call_count == 2, "fell back to the per-id route"


@pytest.mark.asyncio
async def test_non_mapping_batch_response_falls_back_rather_than_crashing():
    """Shape drift (or a bare ``AsyncMock`` double) must not poison contexts
    with junk rows — it means "we learned nothing", so ask the old way."""
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem = str(uuid4())
    sc = AsyncMock()
    _install_entity_storage(sc, {mem: [{"entity_id": "e1", "role": "subject"}]})
    sc.get_entities_by_ids = AsyncMock(return_value=["not", "a", "mapping"])

    out = await _fetch_entity_contexts(sc, [mem], TENANT)

    assert out[mem] == [
        {
            "name": "name-e1",
            "entity_type": "project",
            "role": "subject",
            "entity_id": "e1",
        }
    ]
    sc.get_entity.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_batch_response_is_an_answer_not_a_failure():
    """None of the ids belong to this tenant. That is a valid ``{}``; retrying
    per-id would make N calls to be told the same thing N times."""
    from core_api.services.contradiction_detector import _fetch_entity_contexts

    mem = str(uuid4())
    sc = AsyncMock()
    _install_entity_storage(sc, {mem: [{"entity_id": "e1", "role": "subject"}]})
    sc.get_entities_by_ids = AsyncMock(return_value={})

    out = await _fetch_entity_contexts(sc, [mem], TENANT)

    assert out == {mem: []}
    sc.get_entity.assert_not_called()


# ---------------------------------------------------------------------------
# Storage client wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_client_posts_the_id_list_to_the_batch_route():
    from core_api.clients.storage_client import CoreStorageClient

    sc = CoreStorageClient()
    seen: dict = {}

    async def fake_post(path, data=None, *, read=False, **_kw):
        seen.update(path=path, data=data, read=read)
        return {}

    sc._post = fake_post  # type: ignore[method-assign]
    await sc.get_entities_by_ids(["e1", "e2"], TENANT)

    assert seen["path"] == "/entities/by-ids"
    assert seen["data"] == {"entity_ids": ["e1", "e2"], "tenant_id": TENANT}
    # Pairs with ``get_entity``, which has always read from the replica.
    assert seen["read"] is True
