"""Tests for the tamper-evident audit hash chain (eToro governance).

Unit tests cover the pure chain primitives (canonicalization, hashing,
genesis, the PII-safe guard). Integration tests exercise the chained insert +
verifier against PostgreSQL via ``PostgresService``: chain continuity across
batches, concurrent same-tenant serialization, cross-tenant independence, and
tamper / seq-gap / tail-truncation detection.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text

from core_storage_api.services.audit_chain import (
    GENESIS_PREV_HASH,
    PIIInAuditError,
    _canonical_resource_id,
    assert_pii_safe,
    canonical_event,
    compute_event_hash,
)
from core_storage_api.services.postgres_service import PostgresService, get_session


def _tenant() -> str:
    return f"test-tenant-chain-{uuid4().hex[:8]}"


def _event(
    action: str, *, agent_id: str | None = None, detail: dict | None = None
) -> dict:
    return {
        "tenant_id": None,  # set by the caller / _insert
        "agent_id": agent_id,
        "action": action,
        "resource_type": "memory",
        "resource_id": None,
        "detail": detail,
    }


async def _insert(svc: PostgresService, tenant: str, events: list[dict]) -> None:
    for e in events:
        e["tenant_id"] = tenant
    await svc.audit_add_batch_chained(events)


# ── Unit: chain primitives (no DB) ───────────────────────────────────


def test_canonical_event_is_key_order_independent():
    a = canonical_event(
        tenant_id="t",
        seq=1,
        agent_id="ag",
        action="pii_mask",
        resource_type="memory",
        resource_id=None,
        detail={"b": 2, "a": 1},
        created_at_iso="2026-06-15T00:00:00.000000Z",
    )
    b = canonical_event(
        tenant_id="t",
        seq=1,
        agent_id="ag",
        action="pii_mask",
        resource_type="memory",
        resource_id=None,
        detail={"a": 1, "b": 2},
        created_at_iso="2026-06-15T00:00:00.000000Z",
    )
    assert a == b  # sort_keys makes detail key order irrelevant


def _canon_detail(detail):
    return canonical_event(
        tenant_id="t",
        seq=1,
        agent_id=None,
        action="x",
        resource_type="memory",
        resource_id=None,
        detail=detail,
        created_at_iso="2026-06-15T00:00:00.000000Z",
    )


def test_canonical_event_normalises_integral_floats():
    # An integral float (1.0) and an int (1) must hash identically, so a JSONB
    # round-trip that renders one as the other can't trip a false
    # event_hash_mismatch on an intact row. Non-integral floats are preserved.
    assert _canon_detail({"n": 1.0, "xs": [2.0, 3]}) == _canon_detail(
        {"n": 1, "xs": [2, 3]}
    )
    assert _canon_detail({"n": 1.5}) != _canon_detail({"n": 1})
    # bool must not be coerced to int (True != 1 in the hash).
    assert _canon_detail({"b": True}) != _canon_detail({"b": 1})


def test_canonical_event_normalises_resource_id_case_and_type():
    # The writer may pass resource_id as a raw (uppercase) string while the
    # verifier passes the uuid.UUID read back from the column; canonical_event
    # must hash both identically so the chain doesn't look tampered.
    u = uuid4()
    kw = dict(
        tenant_id="t",
        seq=1,
        agent_id=None,
        action="x",
        resource_type="memory",
        detail=None,
        created_at_iso="2026-06-15T00:00:00.000000Z",
    )
    from_upper_str = canonical_event(resource_id=str(u).upper(), **kw)
    from_uuid_obj = canonical_event(resource_id=u, **kw)
    assert from_upper_str == from_uuid_obj


def test_event_hash_is_deterministic_and_links_history():
    canon = canonical_event(
        tenant_id="t",
        seq=1,
        agent_id=None,
        action="x",
        resource_type="memory",
        resource_id=None,
        detail=None,
        created_at_iso="2026-06-15T00:00:00.000000Z",
    )
    h1 = compute_event_hash(canon, GENESIS_PREV_HASH)
    assert h1 == compute_event_hash(canon, GENESIS_PREV_HASH)
    assert len(h1) == 32
    # A different prev_hash yields a different event hash — the chain binds
    # every prior event into each new one.
    assert compute_event_hash(canon, h1) != h1
    assert GENESIS_PREV_HASH == b"\x00" * 32


def test_assert_pii_safe_rejects_raw_card_and_ssn():
    with pytest.raises(PIIInAuditError):
        assert_pii_safe({"note": "card 4111 1111 1111 1111 leaked"})
    with pytest.raises(PIIInAuditError):
        assert_pii_safe({"nested": {"deep": "ssn 123-45-6789 here"}})


def test_assert_pii_safe_allows_the_real_detail_shape():
    # The actual governance audit detail: category labels, span offsets, and
    # hash tokens — never raw values. Must not raise.
    assert_pii_safe(
        {
            "action": "pii_mask",
            "categories": ["credit_card", "email"],
            "spans": [[10, 26], [40, 55]],
            "finding_count": 2,
            "content_sha256": "a1b2c3" + "0" * 58,  # 64-char hex hash token
            "value_hash": "hmac-sha256:" + "9" * 40,
        }
    )
    assert_pii_safe(None)
    assert_pii_safe({})


def test_assert_pii_safe_allows_benign_numeric_ids():
    # The card guard is conservative (matches only separated 4-4-4-4) so benign
    # numeric strings in detail — order IDs, transaction refs, phone numbers —
    # are NOT flagged. A false positive would roll back the whole batch and gap
    # the tamper-evident chain, which is worse than a miss (the authoritative
    # detector is the write-path governance library, not this backstop).
    assert_pii_safe({"transaction_ref": "2026061512345678"})  # 16 bare digits
    assert_pii_safe({"order_id": "1234567890123456"})
    assert_pii_safe({"phone": "+14155552671"})
    # A clearly card-shaped (separated 4-4-4-4) value still trips the guard.
    with pytest.raises(PIIInAuditError):
        assert_pii_safe({"leak": "4111 1111 1111 1111"})


def test_canonical_resource_id_normalises_case_and_type():
    # The resource_id column is a UUID, so verify reads back a lowercase
    # uuid.UUID; the write path must hash the same canonical form regardless of
    # how the id arrived (raw uppercase string, UUID object, or None).
    upper = "12345678-1234-5678-1234-567890ABCDEF"
    canonical = "12345678-1234-5678-1234-567890abcdef"
    assert _canonical_resource_id(upper) == canonical
    assert _canonical_resource_id(canonical) == canonical
    u = uuid4()
    assert _canonical_resource_id(u) == str(u)  # UUID object → canonical str
    assert _canonical_resource_id(None) is None
    assert _canonical_resource_id("") is None  # falsy collapses to None
    assert _canonical_resource_id("not-a-uuid") == "not-a-uuid"  # non-UUID left as-is


# ── Integration: chained insert + verify ─────────────────────────────


async def test_chain_verifies_batch_event_with_uppercase_uuid_resource_id():
    # Regression: a batch event whose resource_id arrives as an UPPERCASE UUID
    # string must still verify. The column is a UUID, so verify reads it back
    # lowercase; before canonicalizing the write-time hash this produced a
    # spurious event_hash_mismatch.
    svc = PostgresService()
    tenant = _tenant()
    ev = {**_event("a"), "resource_id": str(uuid4()).upper()}
    await _insert(svc, tenant, [ev])
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 1


async def test_chain_single_batch_verifies_with_genesis():
    svc = PostgresService()
    tenant = _tenant()
    await _insert(svc, tenant, [_event("a"), _event("b"), _event("c")])

    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 3
    assert res["head_seq"] == 3

    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT seq, prev_hash, event_hash FROM audit_log "
                    "WHERE tenant_id=:t ORDER BY seq"
                ),
                {"t": tenant},
            )
        ).all()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert bytes(rows[0][1]) == GENESIS_PREV_HASH  # seq=1 chains onto genesis
    assert bytes(rows[1][1]) == bytes(rows[0][2])  # each prev_hash == prior event_hash
    assert bytes(rows[2][1]) == bytes(rows[1][2])


async def test_chain_continuity_across_batches():
    svc = PostgresService()
    tenant = _tenant()
    await _insert(svc, tenant, [_event("a"), _event("b")])
    await _insert(svc, tenant, [_event("c")])  # second batch chains onto the first
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 3
    assert res["head_seq"] == 3


async def test_single_audit_add_joins_the_chain():
    # The single-event path (sync fallback / keystone audit) must chain too —
    # otherwise it would write NULL-hash rows that break verification.
    svc = PostgresService()
    tenant = _tenant()
    await svc.audit_add(tenant_id=tenant, action="single", resource_type="memory")
    await svc.audit_add(tenant_id=tenant, action="single2", resource_type="memory")
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["head_seq"] == 2


async def test_batch_with_multiple_tenants_chains_each_independently():
    svc = PostgresService()
    t1, t2 = _tenant(), _tenant()
    events = [
        {**_event("a"), "tenant_id": t1},
        {**_event("x"), "tenant_id": t2},
        {**_event("b"), "tenant_id": t1},
    ]
    await svc.audit_add_batch_chained(events)
    assert (await svc.audit_verify_chain(t1))["head_seq"] == 2
    assert (await svc.audit_verify_chain(t2))["head_seq"] == 1


async def test_chains_are_independent_per_tenant_when_interleaved():
    svc = PostgresService()
    t1, t2 = _tenant(), _tenant()
    await _insert(svc, t1, [_event("a")])
    await _insert(svc, t2, [_event("x")])
    await _insert(svc, t1, [_event("b")])
    await _insert(svc, t2, [_event("y")])
    r1 = await svc.audit_verify_chain(t1)
    r2 = await svc.audit_verify_chain(t2)
    assert r1["valid"] and r1["head_seq"] == 2
    assert r2["valid"] and r2["head_seq"] == 2


async def test_concurrent_same_tenant_inserts_serialize():
    # 5 concurrent batches of 4 events for ONE tenant. The audit_chain_head
    # FOR UPDATE lock must serialize them into a single gap-free chain (no seq
    # collisions, no lost events) despite the concurrency.
    svc = PostgresService()
    tenant = _tenant()

    async def one(i: int) -> None:
        await _insert(svc, tenant, [_event(f"a{i}-{j}") for j in range(4)])

    await asyncio.gather(*(one(i) for i in range(5)))

    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True, res
    assert res["verified_count"] == 20
    assert res["head_seq"] == 20


async def test_verify_detects_tampered_row():
    svc = PostgresService()
    tenant = _tenant()
    await _insert(svc, tenant, [_event("a"), _event("b"), _event("c")])
    # Mutate seq=2's action — the hash binds it, so verification must fail there.
    async with get_session() as s:
        await s.execute(
            text("UPDATE audit_log SET action='tampered' WHERE tenant_id=:t AND seq=2"),
            {"t": tenant},
        )
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is False
    assert res["first_broken"]["seq"] == 2
    assert res["first_broken"]["reason"] == "event_hash_mismatch"
    assert res["verified_count"] == 1  # seq=1 verified before the break


async def test_verify_detects_seq_gap():
    svc = PostgresService()
    tenant = _tenant()
    await _insert(svc, tenant, [_event("a"), _event("b"), _event("c")])
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM audit_log WHERE tenant_id=:t AND seq=2"), {"t": tenant}
        )
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is False
    assert res["first_broken"]["reason"] == "seq_gap"
    assert res["first_broken"]["seq"] == 3  # seq=3 found where 2 expected


async def test_verify_detects_tail_truncation():
    svc = PostgresService()
    tenant = _tenant()
    await _insert(svc, tenant, [_event("a"), _event("b"), _event("c")])
    # Delete the last row: the walk of 1..2 verifies, but the head still
    # remembers seq=3 — only the head-vs-tail check catches this.
    async with get_session() as s:
        await s.execute(
            text("DELETE FROM audit_log WHERE tenant_id=:t AND seq=3"), {"t": tenant}
        )
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is False
    assert res["first_broken"]["reason"] == "tail_truncated"
    assert res["first_broken"]["head_seq"] == 3
    assert res["first_broken"]["chain_seq"] == 2


async def test_chained_insert_rejects_raw_pii_and_persists_nothing():
    svc = PostgresService()
    tenant = _tenant()
    with pytest.raises(PIIInAuditError):
        await _insert(
            svc, tenant, [_event("leak", detail={"raw": "4111 1111 1111 1111"})]
        )
    # The transaction rolled back on the raise — no head row, no audit rows.
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 0


async def test_empty_batch_is_a_noop():
    svc = PostgresService()
    await svc.audit_add_batch_chained([])  # must not raise or open a session


async def test_chained_insert_dedups_replayed_client_event_id():
    """A retried bulk flush re-sends the same client_event_ids (lost-ack); the
    chain must dedup the already-committed ones — no duplicate rows, seqs stay
    contiguous (no gap), and the chain stays valid — while a genuinely new event
    in the replay still appends."""
    svc = PostgresService()
    tenant = _tenant()
    ev1 = {**_event("create"), "tenant_id": tenant, "client_event_id": uuid4().hex}
    ev2 = {**_event("update"), "tenant_id": tenant, "client_event_id": uuid4().hex}
    ev3 = {**_event("delete"), "tenant_id": tenant, "client_event_id": uuid4().hex}

    # First flush commits ev1 + ev2.
    await svc.audit_add_batch_chained([dict(ev1), dict(ev2)])
    # Lost-ack retry: re-send ev1 + ev2 (already chained) plus a new ev3.
    await svc.audit_add_batch_chained([dict(ev1), dict(ev2), dict(ev3)])

    async with get_session() as s:
        seqs = (
            (
                await s.execute(
                    text("SELECT seq FROM audit_log WHERE tenant_id = :t ORDER BY seq"),
                    {"t": tenant},
                )
            )
            .scalars()
            .all()
        )
    # ev1, ev2 once each + ev3 = 3 rows with contiguous seqs (the dedup must not
    # leave a gap — survivors are seq'd after the existing ones are filtered).
    assert seqs == [1, 2, 3]
    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 3


async def test_dedup_within_a_single_batch():
    """A client_event_id duplicated within ONE batch is chained once (defends
    against a caller accidentally re-listing the same event)."""
    svc = PostgresService()
    tenant = _tenant()
    ev = {**_event("create"), "tenant_id": tenant, "client_event_id": uuid4().hex}

    await svc.audit_add_batch_chained([dict(ev), dict(ev)])

    res = await svc.audit_verify_chain(tenant)
    assert res["valid"] is True
    assert res["verified_count"] == 1
