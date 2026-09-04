"""Tests for CAURA-602 — bulk write per-attempt idempotency + 207 contract.

The earlier CAURA-599 reconcile-on-ReadTimeout path is gone; per-item
``client_request_id`` (derived from ``X-Bulk-Attempt-Id``) plus the
storage-side partial unique index are what makes the bulk path
retry-safe at the row level. These tests cover the new contract:

- Short-content surfaces as per-item ``status="error"`` and triggers a
  207 mixed response when other items succeed.
- Whole-batch error returns 422.
- ``X-Bulk-Attempt-Id`` is required and validated.
- A retry of the same attempt id returns ``duplicate_attempt`` for
  every previously-committed row, with the canonical id — no silent
  creates.
- Same content via a *different* attempt id surfaces as
  ``duplicate_content`` (the legacy dedup).
- A bulk-budget burn returns 504 without recording an idempotency
  receipt, so the next retry can resolve cleanly.
"""

import asyncio
import time

import pytest

from core_api.constants import MAX_CONTENT_LENGTH
from tests.conftest import get_test_auth, uid

pytestmark = pytest.mark.asyncio


def _attempt_id(prefix: str) -> str:
    return f"{prefix}-{uid()}"


# ── Per-item validation ──


async def test_short_content_in_mixed_batch_returns_207(client):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"short-{uid()}",
        "items": [
            {"content": f"well-formed content one {uid()}"},
            {"content": "hi"},  # below CRYSTALLIZER_SHORT_CONTENT_CHARS
            {"content": f"well-formed content two {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("mixed")},
    )
    # 207 Multi-Status: at least one created + at least one error.
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 2
    assert data["errors"] == 1
    assert data["duplicates"] == 0

    by_index = {r["index"]: r for r in data["results"]}
    assert by_index[0]["status"] == "created"
    assert by_index[1]["status"] == "error"
    assert "too short" in by_index[1]["error"]
    assert by_index[2]["status"] == "created"
    # Every result carries its server-derived per-item attempt id —
    # callers can use this to correlate with retries.
    for r in data["results"]:
        assert r["client_request_id"]
        assert r["client_request_id"].endswith(f":{r['index']}")


async def test_all_short_content_batch_returns_200(client):
    """Every item rejected on merit: the request itself was fine, so
    we return 200 with ``errors == n``. FastAPI's automatic 422 covers
    *request-body* validation; this route deliberately doesn't shadow
    it for per-item business-logic rejections (CAURA-602).
    """
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"all-short-{uid()}",
        "items": [{"content": "hi"}, {"content": "ok"}, {"content": "yo"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("all-short")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 0
    assert data["errors"] == 3
    assert data["duplicates"] == 0
    assert all(r["status"] == "error" for r in data["results"])


async def test_oversized_content_in_mixed_batch_returns_207(client):
    """One item whose ``content`` exceeds ``MAX_CONTENT_LENGTH`` must NOT
    422 the whole batch (the pre-fix behaviour, caused by the schema-level
    ``max_length`` on ``BulkMemoryItem.content``). Instead the valid items
    are written and the oversized item surfaces as a per-item
    ``status="error"`` — same additive-tolerant contract as short-content.
    """
    tenant_id, headers = get_test_auth()
    oversized = "x" * (MAX_CONTENT_LENGTH + 1)
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"oversized-{uid()}",
        "items": [
            {"content": f"well-formed content one {uid()}"},
            {"content": oversized},  # over MAX_CONTENT_LENGTH
            {"content": f"well-formed content two {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("oversized")},
    )
    # 207 Multi-Status: two created + one error — NOT a whole-batch 422.
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 2
    assert data["errors"] == 1
    assert data["duplicates"] == 0

    by_index = {r["index"]: r for r in data["results"]}
    assert by_index[0]["status"] == "created"
    assert by_index[0]["id"]
    assert by_index[1]["status"] == "error"
    assert "exceeds" in by_index[1]["error"]
    assert str(MAX_CONTENT_LENGTH) in by_index[1]["error"]
    # The oversized item was never written — no id on the error row.
    assert by_index[1]["id"] is None
    assert by_index[2]["status"] == "created"
    assert by_index[2]["id"]
    # Server-derived per-item attempt id is present on every row.
    for r in data["results"]:
        assert r["client_request_id"]
        assert r["client_request_id"].endswith(f":{r['index']}")


async def test_all_valid_batch_returns_200(client):
    """An all-valid batch (including an item at exactly ``MAX_CONTENT_LENGTH``,
    the boundary) still returns 200 with every item ``created`` — the fix
    doesn't perturb the happy path."""
    tenant_id, headers = get_test_auth()
    # Exactly MAX_CONTENT_LENGTH chars: (MAX-9) filler + "-" + 8-char uid.
    at_cap = ("y" * (MAX_CONTENT_LENGTH - 9)) + f"-{uid()}"
    assert len(at_cap) == MAX_CONTENT_LENGTH
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"all-valid-{uid()}",
        "items": [
            {"content": f"valid alpha {uid()}"},
            {"content": at_cap},
            {"content": f"valid gamma {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("all-valid")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 3
    assert data["errors"] == 0
    assert data["duplicates"] == 0
    assert all(r["status"] == "created" and r["id"] for r in data["results"])


async def test_empty_content_in_mixed_batch_returns_207(client):
    """An empty-content item must NOT 422 the whole batch either. Dropping the
    schema-level ``min_length=1`` from ``BulkMemoryItem.content`` lets an empty
    item reach the per-item short-content check (``< CRYSTALLIZER_SHORT_CONTENT_CHARS``
    = 10, which subsumes empty), so it surfaces as a per-item ``status="error"``
    while valid siblings are written — the same additive-tolerant contract as
    oversized content, closing the empty-vs-oversized asymmetry."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"empty-{uid()}",
        "items": [
            {"content": f"well-formed content one {uid()}"},
            {"content": ""},  # empty — previously 422'd the entire batch
            {"content": f"well-formed content two {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("empty")},
    )
    # 207 Multi-Status: two created + one error — NOT a whole-batch 422.
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 2
    assert data["errors"] == 1
    assert data["duplicates"] == 0

    by_index = {r["index"]: r for r in data["results"]}
    assert by_index[0]["status"] == "created" and by_index[0]["id"]
    assert by_index[1]["status"] == "error"
    # The empty item was never written — no id on the error row.
    assert by_index[1]["id"] is None
    assert by_index[2]["status"] == "created" and by_index[2]["id"]


async def test_bad_weight_and_status_in_mixed_batch_return_207(client):
    """Out-of-range ``weight`` and an unknown ``status`` must each surface as a
    per-item error, not a whole-batch 422: those schema-level ``Field``
    constraints (``ge``/``le``, ``pattern``) were moved to per-item checks in
    ``create_memories_bulk`` for additive-tolerance, like content. Valid
    siblings are still written. (``memory_type`` is deferred — still a typed
    enum on the schema.)"""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"badfields-{uid()}",
        "items": [
            {"content": f"valid one {uid()}"},
            {"content": f"bad weight {uid()}", "weight": 1.5},  # > 1.0
            {
                "content": f"bad status {uid()}",
                "status": "nonsense",
            },  # not a lifecycle status
            {"content": f"valid two {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("badfields")},
    )
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 2
    assert data["errors"] == 2
    by_index = {r["index"]: r for r in data["results"]}
    assert by_index[0]["status"] == "created" and by_index[0]["id"]
    assert by_index[1]["status"] == "error" and by_index[1]["id"] is None
    assert "weight" in by_index[1]["error"]
    assert by_index[2]["status"] == "error" and by_index[2]["id"] is None
    assert "status" in by_index[2]["error"]
    assert by_index[3]["status"] == "created" and by_index[3]["id"]


async def test_item_failing_multiple_validations_aggregates_errors(client):
    """An item violating several constraints at once surfaces ONE per-item error
    row listing all of them (not just the first), so a caller can fix everything
    in a single round-trip rather than discovering problems one at a time."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"multi-{uid()}",
        "items": [
            {"content": f"valid {uid()}"},
            {"content": f"both bad {uid()}", "weight": 1.5, "status": "nonsense"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("multi")},
    )
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 1
    assert data["errors"] == 1
    err = {r["index"]: r for r in data["results"]}[1]
    assert err["status"] == "error" and err["id"] is None
    # Both problems reported in the single aggregated message.
    assert "weight" in err["error"]
    assert "status" in err["error"]


async def test_bad_memory_type_in_mixed_batch_returns_207(client):
    """An unknown memory_type on one item is a per-item 207 error (memory_type is
    now a plain str on BulkMemoryItem, validated against MEMORY_TYPES in the
    service) rather than a whole-batch 422. A VALID explicit memory_type still
    flows through and is written, and valid siblings are unaffected."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"badtype-{uid()}",
        "items": [
            {"content": f"valid explicit type {uid()}", "memory_type": "decision"},
            {"content": f"bad type {uid()}", "memory_type": "not_a_type"},
            {"content": f"valid default type {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("badtype")},
    )
    assert resp.status_code == 207
    data = resp.json()
    assert data["created"] == 2
    assert data["errors"] == 1
    by_index = {r["index"]: r for r in data["results"]}
    assert by_index[0]["status"] == "created" and by_index[0]["id"]
    assert by_index[1]["status"] == "error" and by_index[1]["id"] is None
    assert "memory_type" in by_index[1]["error"]
    assert by_index[2]["status"] == "created" and by_index[2]["id"]


# ── X-Bulk-Attempt-Id contract ──


async def test_missing_bulk_attempt_id_rejected(client):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"miss-attempt-{uid()}",
        "items": [{"content": f"hello {uid()}"}],
    }
    resp = await client.post("/api/v1/memories/bulk", json=body, headers=headers)
    assert resp.status_code == 400
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


async def test_malformed_bulk_attempt_id_rejected(client):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"bad-attempt-{uid()}",
        "items": [{"content": f"hello {uid()}"}],
    }
    # Spaces / non-allowed chars are out — the partial-unique key has
    # to match a strict pattern so SDK bugs don't pollute the index.
    # ASCII-only here on purpose: httpx ASCII-encodes header values, and
    # the regex alone is what's under test.
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": "has spaces and slashes/"},
    )
    assert resp.status_code == 400


# ── Per-attempt idempotency: the silent-create eliminator ──


async def test_retry_same_attempt_id_returns_duplicate_attempt(client):
    """Send a batch, then send the *exact same payload + attempt id* a
    second time. Every row from the first call must come back as
    ``duplicate_attempt`` with the canonical id — no second insert,
    no silent create."""
    tenant_id, headers = get_test_auth()
    attempt_id = _attempt_id("retry")
    contents = [f"retry-test-{uid()}-{i}" for i in range(3)]
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"retry-{uid()}",
        "items": [{"content": c} for c in contents],
    }

    first = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": attempt_id},
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["created"] == 3
    first_ids = [r["id"] for r in first_data["results"]]
    assert all(first_ids)

    # ``Idempotency-Key`` is intentionally absent: we want to exercise
    # the *row-level* recovery path, not the response-replay cache.
    # Production retries hit one or the other; the per-attempt-id path
    # is the harder guarantee and the one that closes the silent-create
    # class.
    second = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": attempt_id},
    )
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["created"] == 0
    # All three rows are duplicates of the previous attempt — counted
    # together in the rolled-up ``duplicates`` total.
    assert second_data["duplicates"] == 3
    assert second_data["errors"] == 0

    second_ids = [r["id"] for r in second_data["results"]]
    assert second_ids == first_ids
    for r in second_data["results"]:
        assert r["status"] == "duplicate_attempt"


async def test_different_attempt_id_same_content_is_duplicate_content(client):
    """A different ``X-Bulk-Attempt-Id`` with overlapping content
    surfaces the existing rows as ``duplicate_content`` (today's
    content-hash dedup), not ``duplicate_attempt``. The two states
    mean different things to the caller."""
    tenant_id, headers = get_test_auth()
    content = f"shared-content-{uid()}"
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"shared-{uid()}",
        "items": [{"content": content}],
    }

    first = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("first")},
    )
    assert first.status_code == 200
    canonical_id = first.json()["results"][0]["id"]

    second = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("second")},
    )
    assert second.status_code == 200
    second_result = second.json()["results"][0]
    assert second_result["status"] == "duplicate_content"
    assert second_result["id"] == canonical_id
    assert second_result["duplicate_of"] == canonical_id


# ── Bulk-budget burn returns 504 without persisting state ──


async def test_bulk_budget_burn_returns_504_with_retry_hint(client, monkeypatch):
    """If ``create_memories_bulk`` exceeds ``bulk_request_timeout_seconds``,
    the route returns 504 and does NOT record an idempotency receipt.
    The retry then runs cleanly against the per-attempt unique index."""
    from core_api import config as cfg
    from core_api.services import memory_service

    # Squeeze the budget so we don't have to actually wait 90s.
    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 0.05)

    real_create = memory_service.create_memories_bulk

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(1.0)
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(memory_service, "create_memories_bulk", slow_create)
    # The route imports ``create_memories_bulk`` by name at module load
    # time, so patch the route's local binding too.
    from core_api.routes import memories as routes_mem

    monkeypatch.setattr(routes_mem, "create_memories_bulk", slow_create)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"slow-{uid()}",
        "items": [{"content": f"slow-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("slow")},
    )
    assert resp.status_code == 504
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


# ── CAURA-599: per-phase storage timeout + broader exception mapping ──


async def test_storage_phase_timeout_returns_504(client, monkeypatch):
    """``storage_bulk_timeout_seconds`` fires before the umbrella when the
    storage roundtrip itself is slow, raising plain ``TimeoutError`` from
    ``asyncio.timeout``. The route maps it to the same 504 contract."""

    from core_api import config as cfg
    from core_api.clients import storage_client as sc_mod

    # Squeeze only the storage-phase cap; leave the umbrella generous so
    # we know the storage timeout — not the umbrella — is what fired.
    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 0.05)
    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 30.0)

    async def slow_create_memories(self, data):
        # Sleep just past the 50ms cap so the timeout fires; no need to
        # waste a full second of test time.
        await asyncio.sleep(0.1)
        return [
            {
                "client_request_id": d["client_request_id"],
                "id": "x",
                "was_inserted": True,
            }
            for d in data
        ]

    monkeypatch.setattr(
        sc_mod.CoreStorageClient, "create_memories", slow_create_memories
    )

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"phase-{uid()}",
        "items": [{"content": f"phase-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("phase")},
    )
    assert resp.status_code == 504
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


async def test_storage_phase_timeout_covers_slot_acquire_wait(client, monkeypatch):
    """Regression for the compound-context-manager ordering: the storage
    timeout must arm BEFORE ``per_tenant_storage_slot`` calls
    ``Semaphore.acquire()``. Otherwise a tenant whose storage-write slots
    are exhausted (other in-flight bulk writes) would queue indefinitely
    and the 40s cap would never fire. Test by holding the only slot via
    a permanently-pending external task; the bulk write should then 504
    inside the timeout window, not hang."""
    import core_api.middleware.per_tenant_concurrency as concurrency_mod
    from core_api import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 30.0)

    tenant_id, headers = get_test_auth()
    # Drain every slot for this tenant on the storage_write semaphore so
    # the route's acquire blocks. Cap is read at semaphore-creation time,
    # so populate the dict directly with a Semaphore(0) — guaranteed to
    # block on acquire — for the (scope, tenant_id) key the route uses.
    saturated = asyncio.Semaphore(0)
    concurrency_mod._TENANT_SEMAPHORES[("storage_write", tenant_id)] = saturated

    try:
        body = {
            "tenant_id": tenant_id,
            "agent_id": f"slot-{uid()}",
            "items": [{"content": f"slot-content-{uid()}"}],
        }
        # Cap the test-side budget at 2x the storage timeout — if the
        # regression returns and the timeout sits INSIDE the semaphore,
        # the request would block until the 30s umbrella fires (still
        # 504, but the test would spend 30s confirming the wrong path).
        # Failing fast at <1s makes the regression unmistakable.
        t0 = time.perf_counter()
        resp = await asyncio.wait_for(
            client.post(
                "/api/v1/memories/bulk",
                json=body,
                headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("slot")},
            ),
            timeout=2.0,
        )
        elapsed = time.perf_counter() - t0
        assert resp.status_code == 504, (
            "storage_bulk_timeout must cover the per_tenant_storage_slot "
            "acquire wait — if it returns 200/207 here, the timeout context "
            "manager is layered INSIDE the semaphore acquire, not outside."
        )
        assert elapsed < 1.0, (
            f"storage_bulk_timeout (0.1s) failed to fire fast — took {elapsed:.2f}s. "
            "Likely regression: timeout context manager is INSIDE the semaphore "
            "acquire, so the umbrella (30s) is what fired instead."
        )
        assert "X-Bulk-Attempt-Id" in resp.json()["detail"]
    finally:
        # Pop the saturated semaphore so subsequent tests see a fresh
        # cap-sized one on next access.
        concurrency_mod._TENANT_SEMAPHORES.pop(("storage_write", tenant_id), None)


async def test_storage_5xx_returns_504_not_500(client, monkeypatch):
    """Storage 5xx (raised by ``raise_for_status``) used to surface as 500
    from core-api. CAURA-599 maps it to 504 so the same retry contract
    applies — the call may have committed without the response landing."""
    import httpx

    from core_api.clients import storage_client as sc_mod

    async def boom_create_memories(self, data):
        # Synthesize what ``resp.raise_for_status()`` produces on 503.
        request = httpx.Request("POST", "https://storage.local/memories/bulk")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr(
        sc_mod.CoreStorageClient, "create_memories", boom_create_memories
    )

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"5xx-{uid()}",
        "items": [{"content": f"5xx-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("5xx")},
    )
    assert resp.status_code == 504
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


async def test_storage_4xx_does_not_get_5xx_recovery_hint(client, monkeypatch):
    """4xx from storage is a request-shape problem the client must fix —
    don't paper over it with a 504/503 retry hint. The handler's
    ``HTTPStatusError`` branch only swallows 5xx; 4xx must escape so the
    bug stays visible. In production FastAPI converts the uncaught
    exception to 500; under TestClient it propagates as a Python
    exception, which is the cleanest portable assertion."""
    import httpx

    from core_api.clients import storage_client as sc_mod

    async def four_oh_four(self, data):
        request = httpx.Request("POST", "https://storage.local/memories/bulk")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(sc_mod.CoreStorageClient, "create_memories", four_oh_four)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"4xx-{uid()}",
        "items": [{"content": f"4xx-content-{uid()}"}],
    }
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.post(
            "/api/v1/memories/bulk",
            json=body,
            headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("4xx")},
        )
    assert exc_info.value.response.status_code == 404


# ── A 5xx storage marks permanent must not be advertised as retryable ──
#
# The 504-with-retry-hint contract above rests on the failure being able to
# clear: a lost response, a restart mid-commit, a slow upstream. When storage
# reports a failure that reproduces identically on every attempt, that contract
# inverts — a compliant client retries forever. Production ran exactly that:
# ~680 requests/hour for 29 hours against a batch that could never compile,
# because nothing at this layer separated "might have committed" from "will
# never commit".


def _permanent_response():
    """A storage response carrying the explicit non-retryable marker.

    Built literally rather than through ``common.permanent_failure`` on purpose:
    this is the WIRE shape, and a test that constructs it with the same helper
    the producer uses would keep passing if that helper renamed a key. The
    storage side's own tests cover the builder.

    Unannotated return, matching this module's convention of importing httpx
    inside the functions that need it rather than at module scope.
    """
    import httpx

    request = httpx.Request("POST", "https://storage.local/memories/bulk")
    return httpx.Response(
        500,
        request=request,
        json={
            "detail": {
                "error": "bulk_row_shape",
                "retryable": False,
                "message": "bulk write failed on an internal row-shape invariant",
                "columns": ["embedded_content_hash"],
            }
        },
    )


def _serving(response, monkeypatch):
    """Make the BULK INSERT call — and only that one — return *response*.

    Patches ``_execute``, which sits BELOW ``_post``, so ``_post``'s real
    ``raise_for_status`` and permanence translation both run. Patching
    ``create_memories`` instead would stub out the very code under test, and
    the tests would pass against a client that never learned to classify
    anything.

    Scoped by ``label`` rather than replacing ``_execute`` wholesale, because a
    single bulk request makes several storage calls before the insert (tenant
    config, dedup, agent upsert). Failing all of them raises from OUTSIDE the
    route's ``try``, so the answer comes from ``upstream_http_error_handler``
    instead of the branch under test — which is how the first draft of these
    tests read 503 where it expected 504, and would just as happily have read
    the right status for the wrong reason.
    """
    from core_api.clients import storage_client as sc_mod

    real_execute = sc_mod.CoreStorageClient._execute

    async def fake_execute(self, do_request, *, retry, label):
        if label == "POST /memories/bulk":
            return response
        return await real_execute(self, do_request, retry=retry, label=label)

    monkeypatch.setattr(sc_mod.CoreStorageClient, "_execute", fake_execute)


async def test_the_marker_is_opt_in_and_only_the_literal_false_counts():
    """Every shape that is not an explicit permanence claim stays retryable.

    Fail-open is deliberate: a 5xx really may have committed rows, and
    suppressing the retry on a guess would strand them with nothing to recover
    them — whereas a needless retry is a no-op, because the per-attempt unique
    index resolves an already-committed row to ``duplicate_attempt``. The
    empty-body case is the one ``test_storage_5xx_returns_504_not_500`` above
    constructs, so this doubles as a regression guard on that test's contract.

    ``async`` purely to satisfy this module's ``pytest.mark.asyncio``
    pytestmark; the function awaits nothing.
    """
    import httpx

    from core_api.clients.storage_client import _storage_permanent

    req = httpx.Request("POST", "https://storage.local/memories/bulk")

    message, fields = _storage_permanent(_permanent_response())
    assert message == "bulk write failed on an internal row-shape invariant"
    # The cause and the divergent columns arrive as data, not as prose.
    assert fields["error"] == "bulk_row_shape"
    assert fields["columns"] == ["embedded_content_hash"]

    for label, response in [
        ("empty body", httpx.Response(503, request=req)),
        (
            "FastAPI's default string detail",
            httpx.Response(500, request=req, json={"detail": "Internal Server Error"}),
        ),
        (
            "dict detail without the key",
            httpx.Response(500, request=req, json={"detail": {"error": "other"}}),
        ),
        (
            "explicitly retryable",
            httpx.Response(500, request=req, json={"detail": {"retryable": True}}),
        ),
        # ``None`` must not read as a permanence claim — the reason the check
        # is ``is False`` rather than a falsy test.
        (
            "null",
            httpx.Response(500, request=req, json={"detail": {"retryable": None}}),
        ),
        (
            "a proxy's HTML error page",
            httpx.Response(502, request=req, text="<html>bad gateway</html>"),
        ),
        ("a body that parses to a list", httpx.Response(500, request=req, json=[1, 2])),
    ]:
        assert _storage_permanent(response) is None, label


async def test_permanent_storage_5xx_does_not_advise_retry(client, monkeypatch):
    """A marked-permanent storage 5xx must break the retry loop, not feed it.

    Asserts the three things a client acts on: a machine-readable code rather
    than an English substring, plain "do not retry" prose, and — critically —
    the ABSENCE of the ``X-Bulk-Attempt-Id`` hint the 504 branch emits, since
    that string is what a complying client keys its retry off.
    """
    _serving(_permanent_response(), monkeypatch)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"perm-{uid()}",
        "items": [{"content": f"perm-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("perm")},
    )

    assert resp.status_code == 500
    payload = resp.json()
    assert payload["error"]["code"] == "PERMANENT_WRITE_FAILURE"
    assert payload["error"]["details"]["columns"] == ["embedded_content_hash"]
    detail = payload["detail"]
    assert "Do NOT retry" in detail
    assert "recover any committed items" not in detail


async def test_unmarked_storage_500_still_advises_retry(client, monkeypatch):
    """The default stays retryable, so this fix cannot strand committed rows.

    Distinct from ``test_storage_5xx_returns_504_not_500``, which uses 503 with
    an empty body: this is a 500 carrying FastAPI's ordinary string ``detail``,
    the shape an unhandled storage exception actually produces. It is the case
    that must NOT be caught by the permanence branch.
    """
    import httpx

    request = httpx.Request("POST", "https://storage.local/memories/bulk")
    _serving(
        httpx.Response(500, request=request, json={"detail": "Internal Server Error"}),
        monkeypatch,
    )

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"unmarked-{uid()}",
        "items": [{"content": f"unmarked-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("unmarked")},
    )

    assert resp.status_code == 504
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


# ── A batch that wrote nothing must not be metered ──
#
# Charging up front is round-trip-neutral for a one-off failure but compounds
# under the retry contract: core-api answers a storage failure by telling the
# client to retry with the same X-Bulk-Attempt-Id, so one un-writable batch was
# charged once per attempt, indefinitely. On the 2026-09-02 incident that ran at
# ~648 attempts/hour for 41 hours against a 100-item batch that never wrote a
# row.


def _as_tenant(monkeypatch, tenant_id):
    """Run the route under a TENANT-scoped credential rather than the admin key.

    Load-bearing for every metering assertion below. ``get_test_auth()`` returns
    the ADMIN key, which yields ``AuthContext(tenant_id=None)``, and the bulk
    route guards metering with ``if auth.tenant_id:`` — so under the default
    fixture the meter is never called at all and a "was not metered" assertion
    passes no matter what the code does. The first draft of these tests did
    exactly that: two of them went green against the unfixed route.

    ``monkeypatch.setitem`` so the override is torn down with the test.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    monkeypatch.setitem(
        app.dependency_overrides,
        get_auth_context,
        lambda: AuthContext(tenant_id=tenant_id, org_role="admin"),
    )


def _metering_recorder(monkeypatch):
    """Record calls to the route's metering call, returning the call list.

    Patches the ROUTE module's binding, not ``usage_service``'s: the route does
    ``from ... import bulk_check_and_increment`` at module load, so rebinding
    the source module would leave the route holding the original — the same trap
    ``test_bulk_budget_burn_returns_504_with_retry_hint`` documents for
    ``create_memories_bulk``.
    """
    from core_api.routes import memories as routes_mem

    calls: list[tuple[str, int]] = []

    async def recorder(tenant_id, count):
        calls.append((tenant_id, count))
        return None

    monkeypatch.setattr(routes_mem, "bulk_check_and_increment", recorder)
    return calls


async def test_a_permanently_failed_bulk_write_is_not_metered(client, monkeypatch):
    """Nothing was written, so nothing may be charged."""
    _as_tenant(monkeypatch, "default")
    calls = _metering_recorder(monkeypatch)
    _serving(_permanent_response(), monkeypatch)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"meter-perm-{uid()}",
        "items": [{"content": f"meter-perm-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("meter-perm")},
    )

    assert resp.status_code == 500
    assert calls == [], f"a batch that wrote nothing was metered: {calls}"


async def test_a_bulk_write_that_504s_is_not_metered(client, monkeypatch):
    """The incident's own shape: storage 5xx mapped to a retryable 504.

    This is the one that compounded — the client is told to retry, so charging
    here bills the same never-written batch once per attempt.
    """
    import httpx

    _as_tenant(monkeypatch, "default")
    calls = _metering_recorder(monkeypatch)
    request = httpx.Request("POST", "https://storage.local/memories/bulk")
    _serving(
        httpx.Response(500, request=request, json={"detail": "Internal Server Error"}),
        monkeypatch,
    )

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"meter-504-{uid()}",
        "items": [{"content": f"meter-504-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("meter-504")},
    )

    assert resp.status_code == 504
    assert calls == [], f"a batch that wrote nothing was metered: {calls}"


async def test_a_successful_bulk_write_is_still_metered_per_item(client, monkeypatch):
    """The control, and the one that stops this becoming a free-writes bug.

    Deleting the charge would satisfy both tests above. This asserts the
    successful path still bills, and still bills ``len(items)`` — the amount is
    unchanged, so the fix is about charging on failure and not a repricing.
    """
    _as_tenant(monkeypatch, "default")
    calls = _metering_recorder(monkeypatch)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"meter-ok-{uid()}",
        "items": [
            {"content": f"meter-ok-alpha-{uid()}"},
            {"content": f"meter-ok-beta-{uid()}"},
            {"content": f"meter-ok-gamma-{uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("meter-ok")},
    )

    assert resp.status_code == 200
    assert calls == [(tenant_id, 3)]


async def test_storage_network_error_returns_503_with_retry_after(client, monkeypatch):
    """A network-level error reaching storage (DNS, connect refused, broken
    pipe) maps to 503 + ``Retry-After`` for clean client backoff."""
    import httpx

    from core_api.clients import storage_client as sc_mod

    async def network_error(self, data):
        request = httpx.Request("POST", "https://storage.local/memories/bulk")
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(sc_mod.CoreStorageClient, "create_memories", network_error)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"net-{uid()}",
        "items": [{"content": f"net-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("net")},
    )
    assert resp.status_code == 503
    # Default ``storage_network_error_retry_after_seconds`` is 5 (config.py).
    assert resp.headers.get("Retry-After") == "5"
    assert "X-Bulk-Attempt-Id" in resp.json()["detail"]


async def test_storage_504_carries_attempt_id_retry_hint(client, monkeypatch):
    """Same recovery contract as the umbrella-timeout path (CAURA-602): the
    storage 5xx → 504 branch surfaces the ``X-Bulk-Attempt-Id`` retry hint
    in the detail message so the client knows the per-attempt unique index
    will resolve any committed rows on retry. (The same-Idempotency-Key
    retry behaviour is governed by the idempotency middleware's pending
    claim window, not the bulk-write recovery contract — covered by the
    middleware's own tests.)"""
    import httpx

    from core_api.clients import storage_client as sc_mod

    async def boom_503(self, data):
        request = httpx.Request("POST", "https://storage.local/memories/bulk")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr(sc_mod.CoreStorageClient, "create_memories", boom_503)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"hint-{uid()}",
        "items": [{"content": f"hint-content-{uid()}"}],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": _attempt_id("hint")},
    )
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "X-Bulk-Attempt-Id" in detail
    assert "recover" in detail.lower()
