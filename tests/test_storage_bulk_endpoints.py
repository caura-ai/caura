"""Integration tests for the storage-side bulk endpoints added in PR1.

Covers four new (and one widened) endpoints used by the P1 / P2 / P5
performance overhaul:

- ``POST /memories/batch-update-status`` — widened with per-row
  ``supersedes_id`` / ``unset_supersedes`` / ``expected_supersedes_id``
- ``POST /memories/bulk-get`` — ordered list with ``None`` for missing
- ``POST /entities/bulk-resolve`` — Phase 1 + Phase 2 in one round-trip
- ``POST /entities/bulk-upsert`` — create + update + race-merge in one txn
- ``POST /entities/links/bulk`` — idempotent link upsert via ON CONFLICT

Tests run against the in-process ``core-storage-api`` ASGI app wired by
``_patch_storage_client`` in conftest.py — backed by the same test
postgres the rest of the suite uses.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test, so concurrent suite runs don't collide."""
    return f"test-tenant-bulk-{uuid4().hex[:8]}"


async def _write_memory(sc, tenant_id: str, content: str = "hello world") -> dict:
    """Create a memory and return the storage-side dict."""
    return await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "agent_id": "test-agent",
            "content": content,
            "memory_type": "fact",
            "embedding": [0.1] * VECTOR_DIM,
            "weight": 0.5,
        }
    )


# ============================================================================
# /memories/batch-update-status — widened with supersedes fields
# ============================================================================


async def test_batch_update_status_backward_compat_2field_shape(sc):
    """Pre-PR1 payload shape — ``{memory_id, status}`` per row — keeps
    working unchanged. Guards against breakage of the existing zero
    callers in tree + any deployed consumer not yet updated."""
    tid = _t()
    a = await _write_memory(sc, tid, "fact A")
    b = await _write_memory(sc, tid, "fact B")

    result = await sc.batch_update_status(
        {
            "updates": [
                {"memory_id": a["id"], "status": "archived"},
                {"memory_id": b["id"], "status": "conflicted"},
            ]
        },
        tenant_id=tid,
    )
    assert result["ok"] is True
    assert result["skipped"] == []

    a_post = await sc.get_memory(a["id"])
    b_post = await sc.get_memory(b["id"])
    assert a_post["status"] == "archived"
    assert b_post["status"] == "conflicted"


async def test_batch_update_status_sets_supersedes(sc):
    tid = _t()
    older = await _write_memory(sc, tid, "older fact")
    newer = await _write_memory(sc, tid, "newer fact")

    result = await sc.batch_update_status(
        {
            "updates": [
                {"memory_id": older["id"], "status": "conflicted"},
                {
                    "memory_id": newer["id"],
                    "status": "active",
                    "supersedes_id": older["id"],
                },
            ]
        },
        tenant_id=tid,
    )
    assert result["ok"] is True
    assert result["skipped"] == []

    newer_post = await sc.get_memory(newer["id"])
    assert newer_post["status"] == "active"
    assert str(newer_post["supersedes_id"]) == str(older["id"])


async def test_batch_update_status_unset_supersedes(sc):
    tid = _t()
    older = await _write_memory(sc, tid, "older")
    newer = await _write_memory(sc, tid, "newer")
    # First set the pointer via the single-row PATCH so the unset case
    # has something to clear.
    await sc.update_memory_status(
        newer["id"], "active", tenant_id=tid, supersedes_id=older["id"]
    )

    result = await sc.batch_update_status(
        {
            "updates": [
                {
                    "memory_id": newer["id"],
                    "status": "active",
                    "unset_supersedes": True,
                },
            ]
        },
        tenant_id=tid,
    )
    assert result["ok"] is True

    newer_post = await sc.get_memory(newer["id"])
    assert newer_post["supersedes_id"] is None


async def test_batch_update_status_cas_skip_on_mismatch(sc):
    """``expected_supersedes_id`` is a CAS gate — a stale caller view
    must not clobber a row that's been updated by another writer."""
    tid = _t()
    older = await _write_memory(sc, tid, "older")
    newer = await _write_memory(sc, tid, "newer")
    other = await _write_memory(sc, tid, "third")
    # Row currently points at ``other``.
    await sc.update_memory_status(
        newer["id"], "conflicted", tenant_id=tid, supersedes_id=other["id"]
    )

    # Caller's stale view: thinks the row points at ``older``.
    result = await sc.batch_update_status(
        {
            "updates": [
                {
                    "memory_id": newer["id"],
                    "status": "active",
                    "unset_supersedes": True,
                    "expected_supersedes_id": older["id"],
                },
            ]
        },
        tenant_id=tid,
    )
    # CAS gate fires: row reported as skipped, status untouched.
    assert newer["id"] in result["skipped"]
    newer_post = await sc.get_memory(newer["id"])
    assert newer_post["status"] == "conflicted"
    assert str(newer_post["supersedes_id"]) == str(other["id"])


# ============================================================================
# /memories/bulk-get — order preservation, None for missing, tenant filter
# ============================================================================


async def test_bulk_get_memories_preserves_input_order(sc):
    tid = _t()
    a = await _write_memory(sc, tid, "A")
    b = await _write_memory(sc, tid, "B")
    c = await _write_memory(sc, tid, "C")

    rows = await sc.bulk_get_memories([c["id"], a["id"], b["id"]])
    assert len(rows) == 3
    assert [r["id"] for r in rows] == [c["id"], a["id"], b["id"]]
    assert rows[0]["content"] == "C"


async def test_bulk_get_memories_returns_none_for_missing(sc):
    tid = _t()
    a = await _write_memory(sc, tid, "exists")
    ghost = str(uuid4())

    rows = await sc.bulk_get_memories([a["id"], ghost])
    assert len(rows) == 2
    assert rows[0]["id"] == a["id"]
    assert rows[1] is None


async def test_bulk_get_memories_tenant_filter_yields_none(sc):
    """If ``tenant_id`` is provided, cross-tenant ids return None (per-row
    fail-closed) rather than leaking another tenant's memory dict."""
    tid_a = _t()
    tid_b = _t()
    m_a = await _write_memory(sc, tid_a, "tenant A's")
    m_b = await _write_memory(sc, tid_b, "tenant B's")

    rows = await sc.bulk_get_memories([m_a["id"], m_b["id"]], tenant_id=tid_a)
    assert rows[0] is not None and rows[0]["id"] == m_a["id"]
    assert rows[1] is None  # cross-tenant → masked


async def test_bulk_get_memories_empty_input(sc):
    assert await sc.bulk_get_memories([]) == []


# ============================================================================
# /entities/bulk-resolve — Phase 1 exact, Phase 2 cosine, miss=None
# ============================================================================


async def _create_entity(sc, tenant_id: str, name: str, *, embedding=None) -> dict:
    return await sc.create_entity(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "entity_type": "person",
            "canonical_name": name,
            "attributes": {},
            "name_embedding": embedding,
        }
    )


async def test_bulk_resolve_phase1_exact_matches(sc):
    tid = _t()
    e1 = await _create_entity(sc, tid, "alice")
    e2 = await _create_entity(sc, tid, "bob")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[
            {
                "input_idx": 0,
                "fleet_id": None,
                "canonical_name": "alice",
                "entity_type": "person",
                "name_embedding": None,
            },
            {
                "input_idx": 1,
                "fleet_id": None,
                "canonical_name": "bob",
                "entity_type": "person",
                "name_embedding": None,
            },
            {
                "input_idx": 2,
                "fleet_id": None,
                "canonical_name": "carol",  # no match
                "entity_type": "person",
                "name_embedding": None,
            },
        ],
        threshold=0.85,
    )
    assert len(results) == 3
    assert results[0]["entity_id"] == e1["id"]
    assert results[0]["matched_by"] == "exact"
    assert results[1]["entity_id"] == e2["id"]
    assert results[2] is None


async def test_bulk_resolve_phase2_similarity_above_threshold(sc):
    tid = _t()
    # Stored entity with a known embedding.
    stored_embedding = [0.1] * VECTOR_DIM
    existing = await _create_entity(sc, tid, "helios-7", embedding=stored_embedding)

    # Query with the same embedding → cosine similarity = 1.0 → match.
    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[
            {
                "input_idx": 0,
                "fleet_id": None,
                "canonical_name": "helios-seven",  # different surface form
                "entity_type": "person",
                "name_embedding": stored_embedding,
            },
        ],
        threshold=0.85,
    )
    assert results[0] is not None
    assert results[0]["entity_id"] == existing["id"]
    assert results[0]["matched_by"] == "similarity"
    assert results[0]["similarity"] >= 0.85


async def test_bulk_resolve_phase2_skipped_when_no_embedding(sc):
    """Item without ``name_embedding`` skips Phase 2 — mirrors entity_service
    upsert_entity line 46. Combined with no exact match → returns None."""
    tid = _t()
    await _create_entity(sc, tid, "existing", embedding=[0.5] * VECTOR_DIM)

    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[
            {
                "input_idx": 0,
                "fleet_id": None,
                "canonical_name": "not-existing",
                "entity_type": "person",
                "name_embedding": None,  # explicitly no embedding
            },
        ],
        threshold=0.85,
    )
    assert results[0] is None


async def test_bulk_resolve_empty_input(sc):
    assert (
        await sc.bulk_resolve_entities(tenant_id=_t(), items=[], threshold=0.85) == []
    )


# ============================================================================
# /entities/bulk-upsert — create + update + race-merge in one transaction
# ============================================================================


async def test_bulk_upsert_mixed_create_and_update(sc):
    tid = _t()
    existing = await _create_entity(sc, tid, "old-name")

    results = await sc.bulk_upsert_entities(
        items=[
            {
                "input_idx": 0,
                "action": "create",
                "tenant_id": tid,
                "fleet_id": None,
                "entity_type": "organization",
                "canonical_name": "brand-new",
                "attributes": {"foo": "bar"},
            },
            {
                "input_idx": 1,
                "action": "update",
                "entity_id": existing["id"],
                "tenant_id": tid,
                "fleet_id": None,
                "entity_type": "person",
                "canonical_name": "old-name",
                "attributes": {"merged": True, "_aliases": ["old-name"]},
            },
        ]
    )

    assert results[0]["action"] == "created"
    assert results[1]["action"] == "updated"
    assert results[1]["entity_id"] == existing["id"]

    # Verify the create landed
    new_entity = await sc.get_entity(results[0]["entity_id"])
    assert new_entity["canonical_name"] == "brand-new"
    assert new_entity["attributes"] == {"foo": "bar"}

    # Verify the update wrote merged attrs
    updated = await sc.get_entity(existing["id"])
    assert updated["attributes"] == {"merged": True, "_aliases": ["old-name"]}


async def test_bulk_upsert_create_race_merges(sc):
    """When ``action=create`` but the natural-key row already exists
    (someone created it between resolve and upsert), the endpoint must
    fall back to update — mirroring ``entity_add``'s IntegrityError
    recovery. Outcome: ``action="merged"``."""
    tid = _t()
    # Pre-create the row that would conflict
    existing = await _create_entity(sc, tid, "concurrent")

    results = await sc.bulk_upsert_entities(
        items=[
            {
                "input_idx": 0,
                "action": "create",
                "tenant_id": tid,
                "fleet_id": None,
                "entity_type": "person",
                "canonical_name": "concurrent",
                "attributes": {"from": "racy-write"},
            },
        ]
    )

    assert results[0]["action"] == "merged"
    assert results[0]["entity_id"] == existing["id"]

    merged = await sc.get_entity(existing["id"])
    assert merged["attributes"] == {"from": "racy-write"}


async def test_bulk_upsert_update_missing_id_reports_missing(sc):
    """Action=update on a deleted/nonexistent entity_id surfaces as
    ``action="missing"`` so the caller can disambiguate."""
    ghost = str(uuid4())
    results = await sc.bulk_upsert_entities(
        items=[
            {
                "input_idx": 0,
                "action": "update",
                "entity_id": ghost,
                "tenant_id": _t(),
                "fleet_id": None,
                "entity_type": "person",
                "canonical_name": "nope",
                "attributes": {},
            },
        ]
    )
    assert results[0]["action"] == "missing"


async def test_bulk_upsert_empty_input(sc):
    assert await sc.bulk_upsert_entities(items=[]) == []


# ============================================================================
# /entities/links/bulk — idempotent (memory_id, entity_id) upsert
# ============================================================================


async def test_bulk_upsert_links_creates_all_new(sc):
    tid = _t()
    m = await _write_memory(sc, tid)
    e1 = await _create_entity(sc, tid, "linked-one")
    e2 = await _create_entity(sc, tid, "linked-two")

    results = await sc.bulk_upsert_entity_links(
        items=[
            {
                "input_idx": 0,
                "memory_id": m["id"],
                "entity_id": e1["id"],
                "role": "subject",
            },
            {
                "input_idx": 1,
                "memory_id": m["id"],
                "entity_id": e2["id"],
                "role": "object",
            },
        ]
    )

    assert len(results) == 2
    assert results[0]["created"] is True
    assert results[1]["created"] is True
    assert results[0]["role"] == "subject"
    assert results[1]["role"] == "object"


async def test_bulk_upsert_links_is_idempotent_on_pk_collision(sc):
    """Two writes for the same (memory_id, entity_id) — second is no-op,
    role from first call is preserved (mirrors find_entity_link → skip)."""
    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "the-entity")

    first = await sc.bulk_upsert_entity_links(
        items=[
            {
                "input_idx": 0,
                "memory_id": m["id"],
                "entity_id": e["id"],
                "role": "subject",
            },
        ]
    )
    assert first[0]["created"] is True

    second = await sc.bulk_upsert_entity_links(
        items=[
            {
                "input_idx": 0,
                "memory_id": m["id"],
                "entity_id": e["id"],
                "role": "object",
            },
        ]
    )
    assert second[0]["created"] is False
    # Prior role wins.
    assert second[0]["role"] == "subject"


async def test_bulk_upsert_links_empty_input(sc):
    assert await sc.bulk_upsert_entity_links(items=[]) == []


# ============================================================================
# Atomicity + correctness regressions
# ============================================================================


async def test_bulk_upsert_links_fk_violation_isolated_per_item(sc):
    """An FK violation on one item must not roll back the others — each
    insert gets its own session, and the failing item is reported with
    ``created=False, error="fk_violation"`` so the caller can keep
    processing."""
    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "real-entity")
    ghost_entity = str(uuid4())  # No matching row → FK violation

    results = await sc.bulk_upsert_entity_links(
        items=[
            {
                "input_idx": 0,
                "memory_id": m["id"],
                "entity_id": e["id"],
                "role": "subject",
            },
            {
                "input_idx": 1,
                "memory_id": m["id"],
                "entity_id": ghost_entity,
                "role": "object",
            },
            {"input_idx": 2, "memory_id": m["id"], "entity_id": e["id"], "role": "alt"},
        ]
    )
    assert len(results) == 3
    # Item 0 succeeded
    assert results[0]["created"] is True
    # Item 1 failed (FK violation), reported per-row
    assert results[1]["created"] is False
    assert results[1].get("error") == "fk_violation"
    # Item 2 ran AFTER the failure and still landed (PK already
    # exists from item 0, so created=False here is the role-preserve path,
    # not the FK error path) — proves prior items weren't rolled back.
    assert results[2]["created"] is False
    assert "error" not in results[2]
    assert results[2]["role"] == "subject"  # role preserved from item 0


async def test_bulk_upsert_links_duplicate_input_pair_keeps_both_slots(sc):
    """Two input items pointing at the same (memory_id, entity_id) pair
    must each get their own slot in the response. Earlier the result map
    was keyed by the PK tuple, so the second slot silently overwrote the
    first; now keyed by ``input_idx``."""
    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "dup-target")

    results = await sc.bulk_upsert_entity_links(
        items=[
            {
                "input_idx": 0,
                "memory_id": m["id"],
                "entity_id": e["id"],
                "role": "subject",
            },
            {
                "input_idx": 1,
                "memory_id": m["id"],
                "entity_id": e["id"],
                "role": "object",
            },
        ]
    )
    assert len(results) == 2
    assert results[0]["input_idx"] == 0
    assert results[1]["input_idx"] == 1
    # First insert wins; both response slots reflect the persisted role.
    assert results[0]["role"] == "subject"
    assert results[1]["role"] == "subject"


async def test_bulk_upsert_invalid_action_rejected_422(sc):
    """An item with action not in {create, update} would otherwise be
    silently dropped (response shorter than input). Router validates."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {
                    "input_idx": 0,
                    "action": "delete",  # invalid
                    "tenant_id": _t(),
                    "fleet_id": None,
                    "entity_type": "person",
                    "canonical_name": "nope",
                    "attributes": {},
                },
            ]
        )
    assert exc.value.response.status_code == 422


async def test_patch_memory_status_returns_404_on_missing(sc):
    """``PATCH /memories/{id}/status`` previously silently 200'd on a
    nonexistent id (the bool return from ``memory_update_status`` was
    ignored). Storage-side now returns 404; the client's ``_patch``
    helper translates 404 → ``None`` (established convention shared
    with ``update_entity`` et al.), so callers see ``None`` instead of
    a silent ``{ok: True}`` they can't distinguish from a real update.
    """
    ghost = str(uuid4())
    result = await sc.update_memory_status(ghost, "active", tenant_id=_t())
    assert result is None


# ============================================================================
# Input validation regressions (router-level)
# ============================================================================


async def test_bulk_upsert_input_idx_out_of_range_rejected_422(sc):
    """``input_idx`` outside [0, len(items)) would crash with IndexError
    inside the service (results list-indexing). Router 422s instead."""
    import httpx

    tid = _t()
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {
                    "input_idx": 99,  # out of range for 1-item batch
                    "action": "create",
                    "tenant_id": tid,
                    "fleet_id": None,
                    "entity_type": "person",
                    "canonical_name": "oob",
                    "attributes": {},
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "input_idx" in exc.value.response.text


async def test_bulk_upsert_input_idx_duplicate_rejected_422(sc):
    """Two items with the same ``input_idx`` would overwrite each other
    in the response list. Router rejects."""
    import httpx

    tid = _t()
    base = {
        "action": "create",
        "tenant_id": tid,
        "fleet_id": None,
        "entity_type": "person",
        "attributes": {},
    }
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {**base, "input_idx": 0, "canonical_name": "a"},
                {**base, "input_idx": 0, "canonical_name": "b"},  # duplicate
            ]
        )
    assert exc.value.response.status_code == 422
    assert "duplicate" in exc.value.response.text


async def test_bulk_resolve_input_idx_out_of_range_rejected_422(sc):
    """Same guard on the resolve endpoint."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_resolve_entities(
            tenant_id=_t(),
            items=[
                {
                    "input_idx": 5,  # out of range
                    "fleet_id": None,
                    "canonical_name": "x",
                    "entity_type": "person",
                    "name_embedding": None,
                },
            ],
            threshold=0.85,
        )
    assert exc.value.response.status_code == 422


async def test_bulk_upsert_update_without_entity_id_rejected_422(sc):
    """``action='update'`` without ``entity_id`` would crash inside the
    service. Router rejects with 422 + the bad input_idx."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {
                    "input_idx": 0,
                    "action": "update",
                    # entity_id intentionally absent
                    "tenant_id": _t(),
                    "fleet_id": None,
                    "entity_type": "person",
                    "canonical_name": "needs-id",
                    "attributes": {},
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "entity_id" in exc.value.response.text


async def test_batch_update_status_malformed_uuid_returns_422(sc):
    """``UUID('not-a-uuid')`` raised ValueError → uncaught 500 before
    this fix; now reframed as a 422 naming the bad item."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.batch_update_status(
            {"updates": [{"memory_id": "not-a-uuid", "status": "active"}]},
            tenant_id=_t(),
        )
    assert exc.value.response.status_code == 422


async def test_batch_update_status_missing_required_key_returns_422(sc):
    """``KeyError`` on a missing ``status`` (or ``memory_id``) field
    also surfaces as 422 instead of 500."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.batch_update_status(
            {"updates": [{"memory_id": str(uuid4())}]},  # missing "status"
            tenant_id=_t(),
        )
    assert exc.value.response.status_code == 422


async def test_batch_update_status_partial_validation_failure_writes_nothing(sc):
    """Validation runs as a pre-pass before any DB write — so a malformed
    item at index K must NOT leave items 0..K-1 committed. Verify by
    sending a valid row followed by a malformed one, then asserting the
    valid row's status is unchanged."""
    import httpx

    tid = _t()
    target = await _write_memory(sc, tid, "untouched")
    original_status = target["status"]

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.batch_update_status(
            {
                "updates": [
                    {"memory_id": target["id"], "status": "archived"},
                    {"memory_id": "garbage", "status": "active"},  # crash pt
                ]
            },
            tenant_id=tid,
        )
    assert exc.value.response.status_code == 422

    # Critical: the valid row in position 0 must NOT have been committed.
    post = await sc.get_memory(target["id"])
    assert post["status"] == original_status, (
        "validation pre-pass failed: status was committed before the "
        f"batch was rejected (expected {original_status!r}, got {post['status']!r})"
    )


async def test_bulk_upsert_links_input_idx_out_of_range_rejected_422(sc):
    """Same input_idx guard as the other bulk endpoints — without it,
    out-of-range / missing input_idx would have crashed in the service."""
    import httpx

    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "test-entity")

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entity_links(
            items=[
                {
                    "input_idx": 7,  # out of range for 1-item batch
                    "memory_id": m["id"],
                    "entity_id": e["id"],
                    "role": "subject",
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "input_idx" in exc.value.response.text


async def test_bulk_upsert_links_input_idx_duplicate_rejected_422(sc):
    """Two items sharing the same ``input_idx`` would overwrite each
    other in ``idx_to_result`` → corrupted response shape."""
    import httpx

    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "dup-target")

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entity_links(
            items=[
                {
                    "input_idx": 0,
                    "memory_id": m["id"],
                    "entity_id": e["id"],
                    "role": "subject",
                },
                {
                    "input_idx": 0,
                    "memory_id": m["id"],
                    "entity_id": e["id"],
                    "role": "object",
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "duplicate" in exc.value.response.text


async def test_bulk_upsert_update_cross_tenant_returns_missing(sc):
    """An ``action='update'`` whose ``entity_id`` belongs to a different
    tenant must NOT silently update the foreign row. The service now
    pins the UPDATE WHERE to ``tenant_id``; mismatched rows surface as
    ``action='missing'`` rather than rewriting another tenant's entity."""
    tid_a = _t()
    tid_b = _t()
    foreign = await _create_entity(sc, tid_b, "foreign-entity")
    foreign_pre = await sc.get_entity(foreign["id"])

    results = await sc.bulk_upsert_entities(
        items=[
            {
                "input_idx": 0,
                "action": "update",
                "entity_id": foreign["id"],
                "tenant_id": tid_a,  # different tenant
                "fleet_id": None,
                "entity_type": "person",
                "canonical_name": "renamed",
                "attributes": {"hijacked": True},
            },
        ]
    )
    assert results[0]["action"] == "missing"

    # Foreign tenant's row stays put — attributes and canonical_name unchanged.
    post = await sc.get_entity(foreign["id"])
    assert post["attributes"] == foreign_pre["attributes"]
    assert post["canonical_name"] == foreign_pre["canonical_name"]


async def test_bulk_resolve_non_numeric_threshold_returns_422(sc):
    """``float('abc')`` would raise ValueError → 500 before this fix;
    router now reframes as 422 naming the bad field."""
    resp = await sc._http.post(
        f"{sc._prefix}/entities/bulk-resolve",
        json={"tenant_id": _t(), "threshold": "abc", "items": []},
        headers=await sc._auth_headers(read=False),
    )
    assert resp.status_code == 422
    assert "threshold" in resp.text


async def test_bulk_resolve_non_int_candidate_limit_returns_422(sc):
    resp = await sc._http.post(
        f"{sc._prefix}/entities/bulk-resolve",
        json={
            "tenant_id": _t(),
            "threshold": 0.85,
            "candidate_limit": "five",
            "items": [],
        },
        headers=await sc._auth_headers(read=False),
    )
    assert resp.status_code == 422
    assert "candidate_limit" in resp.text


# ============================================================================
# Required-field validation per bulk endpoint
# ============================================================================


async def test_bulk_upsert_entities_missing_required_field_returns_422(sc):
    """Missing ``attributes`` would crash inside the service with a
    KeyError → uncaught 500. Router validates required fields up-front."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {
                    "input_idx": 0,
                    "action": "create",
                    "tenant_id": _t(),
                    "fleet_id": None,
                    "entity_type": "person",
                    "canonical_name": "no-attrs",
                    # attributes intentionally absent
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "attributes" in exc.value.response.text


async def test_bulk_resolve_missing_required_field_returns_422(sc):
    """Missing ``entity_type`` would crash with KeyError inside Phase 1."""
    resp = await sc._http.post(
        f"{sc._prefix}/entities/bulk-resolve",
        json={
            "tenant_id": _t(),
            "threshold": 0.85,
            "items": [
                {
                    "input_idx": 0,
                    "canonical_name": "no-type",
                    # entity_type intentionally absent
                },
            ],
        },
        headers=await sc._auth_headers(read=False),
    )
    assert resp.status_code == 422
    assert "entity_type" in resp.text


async def test_bulk_upsert_links_missing_required_field_returns_422(sc):
    """Missing ``role`` would crash inside the link upsert loop."""
    import httpx

    tid = _t()
    m = await _write_memory(sc, tid)
    e = await _create_entity(sc, tid, "rolesless")

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entity_links(
            items=[
                {
                    "input_idx": 0,
                    "memory_id": m["id"],
                    "entity_id": e["id"],
                    # role intentionally absent
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "role" in exc.value.response.text


async def test_batch_update_status_error_detail_omits_raw_item_repr(sc):
    """Sensitive item content (status values, supersedes ids) must not
    leak into 422 error bodies. Detail names ``memory_id`` and exception
    type only — not the full dict repr."""
    resp = await sc._http.post(
        f"{sc._prefix}/memories/batch-update-status",
        json={
            "tenant_id": _t(),
            "updates": [
                {
                    "memory_id": str(uuid4()),
                    # status intentionally absent → KeyError
                    "supersedes_id": "secret-value-must-not-leak",
                },
            ],
        },
        headers=await sc._auth_headers(read=False),
    )
    assert resp.status_code == 422
    body = resp.text
    assert "secret-value-must-not-leak" not in body
    # But the memory_id is still surfaced so the client knows which
    # row was the offender.
    assert "memory_id" in body


async def test_batch_update_status_error_omits_malformed_uuid_value(sc):
    """A ValueError from ``UUID("badly-formed")`` carries the raw input
    in its message ("badly formed hexadecimal UUID string: <value>").
    Detail must NOT echo the exception message back — only the
    exception type + a generic field hint. ``memory_id`` itself is
    echoed back by design (helps client identify the row), so the
    leak case to guard is a bad value in a *different* UUID field."""
    resp = await sc._http.post(
        f"{sc._prefix}/memories/batch-update-status",
        json={
            "tenant_id": _t(),
            "updates": [
                {
                    "memory_id": str(uuid4()),
                    "status": "active",
                    # Bad UUID in supersedes_id, NOT memory_id.
                    "supersedes_id": "secret-uuid-must-not-leak-back",
                },
            ],
        },
        headers=await sc._auth_headers(read=False),
    )
    assert resp.status_code == 422
    body = resp.text
    assert "secret-uuid-must-not-leak-back" not in body
    assert "ValueError" in body
    assert "UUID field" in body


# ============================================================================
# UUID format validation at the router boundary
# ============================================================================


async def test_bulk_upsert_update_non_uuid_entity_id_returns_422(sc):
    """``action='update'`` with a present-but-malformed ``entity_id``
    would crash inside the service at ``UUID(eid)`` and bubble via the
    generic 500 fallback. Router now validates UUID shape up-front."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entities(
            items=[
                {
                    "input_idx": 0,
                    "action": "update",
                    "entity_id": "not-a-uuid",  # malformed
                    "tenant_id": _t(),
                    "fleet_id": None,
                    "entity_type": "person",
                    "canonical_name": "x",
                    "attributes": {},
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "entity_id" in exc.value.response.text


async def test_bulk_upsert_links_non_uuid_memory_id_returns_422(sc):
    """Malformed ``memory_id`` UUID must surface as 422 with the field
    named, not as a generic 500."""
    import httpx

    tid = _t()
    e = await _create_entity(sc, tid, "for-link")

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entity_links(
            items=[
                {
                    "input_idx": 0,
                    "memory_id": "not-a-uuid",  # malformed
                    "entity_id": e["id"],
                    "role": "subject",
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "memory_id" in exc.value.response.text


async def test_bulk_upsert_links_non_uuid_entity_id_returns_422(sc):
    import httpx

    tid = _t()
    m = await _write_memory(sc, tid)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc.bulk_upsert_entity_links(
            items=[
                {
                    "input_idx": 0,
                    "memory_id": m["id"],
                    "entity_id": "not-a-uuid",  # malformed
                    "role": "subject",
                },
            ]
        )
    assert exc.value.response.status_code == 422
    assert "entity_id" in exc.value.response.text
