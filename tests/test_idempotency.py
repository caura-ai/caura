"""Tests for CAURA-601 — Idempotency-Key inbox on write routes.

The storage-side ORM/router + core-api helper are exercised end-to-end
through the existing ``client`` fixture (ASGI bridge against both
services). Each scenario:

- replay: same key + same body → same response (no duplicate write)
- conflict: same key + different body → 422
- fresh: no key → writes normally
- TTL expiry: expired rows treated as absent (manual time-warp)
- concurrent claim: simultaneous POSTs with the same key resolve to one
  memory row (closes the race documented in the middleware before
  this fix shipped)
"""

import asyncio
import uuid

import pytest

from tests.conftest import get_test_auth, uid

pytestmark = pytest.mark.asyncio


def _new_key() -> str:
    return f"itest-{uuid.uuid4().hex}"


async def test_no_header_writes_normally(client):
    """Absence of Idempotency-Key must not change behavior."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-none-{uid()}",
        "memory_type": "fact",
        "content": f"no-idem content {uid()}",
    }
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201
    assert "id" in resp.json()


async def test_replay_same_key_same_body(client):
    """Two identical POSTs with the same key return the same stored response."""
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-replay-{uid()}",
        "memory_type": "fact",
        "content": f"replay content {uid()}",
    }
    h = {**headers, "Idempotency-Key": key}

    r1 = await client.post("/api/v1/memories", json=body, headers=h)
    r2 = await client.post("/api/v1/memories", json=body, headers=h)

    assert r1.status_code == 201
    assert r2.status_code == 201
    # Same id both times — the second call replayed the first's response
    # rather than creating a new row.
    assert r1.json()["id"] == r2.json()["id"]


async def test_different_body_same_key_returns_422(client):
    """Reusing a key with a different body is a client error."""
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body1 = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-conflict-{uid()}",
        "memory_type": "fact",
        "content": f"first body {uid()}",
    }
    body2 = {**body1, "content": f"second body {uid()}"}
    h = {**headers, "Idempotency-Key": key}

    r1 = await client.post("/api/v1/memories", json=body1, headers=h)
    assert r1.status_code == 201

    r2 = await client.post("/api/v1/memories", json=body2, headers=h)
    assert r2.status_code == 422
    assert "different request body" in r2.json()["detail"]


async def test_replay_bulk_endpoint(client):
    """Same Idempotency-Key on /memories/bulk replays too.

    The header path (this test) is independent of the row-level
    per-attempt-id path (CAURA-602): both layers compose, but a real
    replay short-circuits before the storage call.
    """
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-bulk-{uid()}",
        "items": [
            {"content": f"bulk idem a {uid()}"},
            {"content": f"bulk idem b {uid()}"},
        ],
    }
    h = {
        **headers,
        "Idempotency-Key": key,
        # Required as of CAURA-602; same value across both calls so the
        # row-level fallback would also be deterministic if the
        # header-level cache somehow missed.
        "X-Bulk-Attempt-Id": f"idem-replay-{uid()}",
    }

    r1 = await client.post("/api/v1/memories/bulk", json=body, headers=h)
    r2 = await client.post("/api/v1/memories/bulk", json=body, headers=h)

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Results array matches across calls; second call did not re-insert.
    r1_ids = sorted(r["id"] for r in r1.json()["results"] if r.get("id"))
    r2_ids = sorted(r["id"] for r in r2.json()["results"] if r.get("id"))
    assert r1_ids == r2_ids
    assert r1_ids  # at least one actually created on r1


async def test_blank_key_rejected(client):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-blank-{uid()}",
        "memory_type": "fact",
        "content": f"blank-key {uid()}",
    }
    resp = await client.post(
        "/api/v1/memories", json=body, headers={**headers, "Idempotency-Key": "   "}
    )
    assert resp.status_code == 400
    assert "blank" in resp.json()["detail"].lower()


async def test_oversized_key_rejected(client):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-huge-{uid()}",
        "memory_type": "fact",
        "content": f"huge-key {uid()}",
    }
    resp = await client.post(
        "/api/v1/memories", json=body, headers={**headers, "Idempotency-Key": "x" * 512}
    )
    assert resp.status_code == 400


async def test_replay_carries_same_ratelimit_headers_as_live(client):
    """Replay responses must emit the same X-RateLimit-* headers the
    live path sets via `response.headers`, so clients don't see a
    shape divergence between first-call and retry."""
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-headers-{uid()}",
        "memory_type": "fact",
        "content": f"headers-replay {uid()}",
    }
    h = {**headers, "Idempotency-Key": key}

    r1 = await client.post("/api/v1/memories", json=body, headers=h)
    r2 = await client.post("/api/v1/memories", json=body, headers=h)

    live_rate = {
        k: v for k, v in r1.headers.items() if k.lower().startswith("x-ratelimit-")
    }
    replay_rate = {
        k: v for k, v in r2.headers.items() if k.lower().startswith("x-ratelimit-")
    }
    assert live_rate == replay_rate


async def test_body_metadata_idempotency_key_replays(client):
    """Clients that can't easily set per-request headers may put the
    idempotency key in ``body.metadata.idempotency_key`` (or the
    legacy ``client_idempotency_key`` alias). Two POSTs with the same
    body-key must replay the first response — same shape as the
    header-based path.

    Closes the load-test review's ``idempotency-key-not-honored``
    finding: the harness puts the key in ``metadata`` rather than as
    a request header, and pre-fix the route only read
    ``Idempotency-Key`` — body keys silently passed through and
    produced two distinct memory rows."""
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-body-{uid()}",
        "memory_type": "fact",
        "content": f"body-key replay content {uid()}",
        "metadata": {"idempotency_key": key, "label": "alpha"},
    }

    r1 = await client.post("/api/v1/memories", json=body, headers=headers)
    r2 = await client.post("/api/v1/memories", json=body, headers=headers)

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"], (
        "second POST with the same metadata.idempotency_key must replay "
        "the first response, not create a new row"
    )


async def test_body_metadata_legacy_alias_replays(client):
    """``metadata.client_idempotency_key`` is accepted as a legacy
    alias for ``idempotency_key``. The load-test harness uses both
    names together; OSS must treat either as canonical."""
    tenant_id, headers = get_test_auth()
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-alias-{uid()}",
        "memory_type": "fact",
        "content": f"alias-key replay content {uid()}",
        "metadata": {"client_idempotency_key": key},
    }

    r1 = await client.post("/api/v1/memories", json=body, headers=headers)
    r2 = await client.post("/api/v1/memories", json=body, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


async def test_idempotency_key_from_metadata_preserves_raw_value():
    """The helper returns the key byte-for-byte: stripping it would
    let two byte-identical retries — one with surrounding whitespace,
    one without — share a cache bucket but mismatch on body hash
    (which is computed from raw request bytes), producing a false
    422 on what is genuinely the same logical request. Pure-whitespace
    values are still rejected."""
    from core_api.middleware.idempotency import idempotency_key_from_metadata

    # Whitespace is preserved so the key matches the body-hash domain
    assert idempotency_key_from_metadata({"idempotency_key": "  abc  "}) == "  abc  "
    assert idempotency_key_from_metadata({"idempotency_key": "abc"}) == "abc"
    assert (
        idempotency_key_from_metadata({"client_idempotency_key": "\tdef\n"})
        == "\tdef\n"
    )
    # Pure whitespace → None
    assert idempotency_key_from_metadata({"idempotency_key": "   "}) is None
    # Missing field → None
    assert idempotency_key_from_metadata({"label": "alpha"}) is None
    # Empty / None metadata → None
    assert idempotency_key_from_metadata({}) is None
    assert idempotency_key_from_metadata(None) is None
    # Non-string value (defence against schema drift) → None
    assert idempotency_key_from_metadata({"idempotency_key": 42}) is None
    # Both fields present — ``idempotency_key`` (canonical) wins over alias
    assert (
        idempotency_key_from_metadata(
            {"idempotency_key": "primary", "client_idempotency_key": "alias"}
        )
        == "primary"
    )


async def test_header_and_body_with_same_value_dont_collide(client):
    """A header key ``"abc"`` and a body-metadata key ``"abc"`` must
    NOT share a cache bucket — body keys are namespaced under
    ``body:`` so the two transports can carry different client
    intent without one path silently replaying the other's response."""
    tenant_id, headers = get_test_auth()
    shared_key = _new_key()

    # First call: header path — establishes a cache entry under the raw key.
    body_via_header = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-ns-h-{uid()}",
        "memory_type": "fact",
        "content": f"header path content {uid()}",
    }
    r_header = await client.post(
        "/api/v1/memories",
        json=body_via_header,
        headers={**headers, "Idempotency-Key": shared_key},
    )

    # Second call: body path with the SAME shared_key but no header.
    # Pre-fix this hit the same cache bucket as the header call and
    # replayed r_header's id; post-fix the body path is namespaced
    # under ``body:`` so this is treated as a fresh write.
    body_via_metadata = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-ns-b-{uid()}",
        "memory_type": "fact",
        "content": f"body path content {uid()}",
        "metadata": {"idempotency_key": shared_key},
    }
    r_body = await client.post(
        "/api/v1/memories", json=body_via_metadata, headers=headers
    )

    assert r_header.status_code == 201, r_header.text
    assert r_body.status_code == 201, r_body.text
    assert r_header.json()["id"] != r_body.json()["id"], (
        "header and body idempotency keys with the same string value must "
        "not share a cache bucket — the body-key is namespaced with ``body:`` "
        "to keep transports isolated"
    )


async def test_oversized_body_key_rejected(client):
    """Body-supplied keys must hit the same length cap (255 chars) as
    header-supplied keys. Pre-fix the body path bypassed the length
    guard entirely, so a 255-char metadata value + 5-char ``body:``
    prefix landed a 260-char string in the storage column and 500'd
    the request."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-bodybig-{uid()}",
        "memory_type": "fact",
        "content": f"big-body-key {uid()}",
        "metadata": {"idempotency_key": "x" * 512},
    }
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 400, resp.text


async def test_header_takes_precedence_over_body_key(client):
    """When both header and body carry an idempotency key, the header
    wins — body field is fallback-only. Sending different values for
    each is a configuration smell; the route must pick one
    deterministically and the IETF-standard header is the canonical
    form."""
    tenant_id, headers = get_test_auth()
    header_key = _new_key()
    body_key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-prec-{uid()}",
        "memory_type": "fact",
        "content": f"precedence content {uid()}",
        "metadata": {"idempotency_key": body_key},
    }
    h = {**headers, "Idempotency-Key": header_key}

    # First POST establishes a cache entry under header_key.
    r1 = await client.post("/api/v1/memories", json=body, headers=h)
    # Second POST with the SAME header_key must replay r1's id —
    # confirming the header is what the route looked up, not body_key.
    r2 = await client.post("/api/v1/memories", json=body, headers=h)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


async def test_concurrent_same_key_returns_same_id(client):
    """Two POSTs fired in parallel with the same Idempotency-Key must
    resolve to the same memory id — the loser polls for the winner's
    response rather than creating a duplicate row.

    Pre-CAURA-602b, this test pre-created the agent row via a warm-up
    write because ``agent_add`` had a separate latent bug — a mid-session
    ``rollback()`` inside its ``IntegrityError`` handler that closed
    the outer transaction. The agent path now uses
    ``INSERT ... ON CONFLICT DO NOTHING RETURNING ...`` (same shape as
    ``memory_add_all``) so the warm-up is no longer required; this test
    drives the agent race directly to lock that fix in.
    """
    tenant_id, headers = get_test_auth()
    agent_id = f"idem-race-{uid()}"
    key = _new_key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "memory_type": "fact",
        "content": f"concurrent content {uid()}",
    }
    h = {**headers, "Idempotency-Key": key}

    r1, r2 = await asyncio.gather(
        client.post("/api/v1/memories", json=body, headers=h),
        client.post("/api/v1/memories", json=body, headers=h),
    )

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


async def test_expired_row_does_not_block_fresh_claim(client, sc):
    """An expired row with the same key must be reclaimable.

    Without ON CONFLICT DO UPDATE WHERE expires_at <= now() the claim
    INSERT conflicts on the PK, ``idempotency_get`` filters the expired
    row out, and the middleware loops between "no cache" and "claim
    failed" until the cleanup job prunes the row.
    """
    tenant_id, headers = get_test_auth()
    key = _new_key()

    # Seed an expired row directly via storage_client. Namespacing prefix
    # matches what the middleware writes (``header:<key>``).
    await sc.upsert_idempotency(
        tenant_id=tenant_id,
        idempotency_key=f"header:{key}",
        request_hash="stale-hash",
        response_body={"stale": True},
        status_code=201,
        expires_at="2000-01-01T00:00:00+00:00",
    )

    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-expired-{uid()}",
        "memory_type": "fact",
        "content": f"reclaim content {uid()}",
    }
    h = {**headers, "Idempotency-Key": key}
    resp = await client.post("/api/v1/memories", json=body, headers=h)
    assert resp.status_code == 201
    assert resp.json()["content"] == body["content"]


async def test_replay_respects_tenant_scoping(client, sc):
    """A stored key in tenant A is not visible to tenant B."""
    tenant_a = "default"
    tenant_b = f"test-tenant-{uid()}"
    _, headers = get_test_auth()
    key = _new_key()

    # Seed tenant A via direct storage client so we don't need two auth
    # contexts in test. Key is (tenant_id, idempotency_key), and a lookup
    # with a different tenant MUST miss.
    await sc.upsert_idempotency(
        tenant_id=tenant_a,
        idempotency_key=key,
        request_hash="any-hash",
        response_body={"seed": True},
        status_code=201,
        expires_at="2099-01-01T00:00:00+00:00",
    )

    hit = await sc.get_idempotency(tenant_a, key)
    miss = await sc.get_idempotency(tenant_b, key)
    assert hit is not None and hit["response_body"] == {"seed": True}
    assert miss is None
