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
