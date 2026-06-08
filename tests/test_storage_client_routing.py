"""Tests for CAURA-591 Part B — core-api storage client reader/writer split.

When ``CORE_STORAGE_READ_URL`` is set, the client routes GET + tagged-read
POST calls to the reader URL. Everything else (including un-tagged POST,
PATCH, DELETE) still goes to the writer URL. Empty ``CORE_STORAGE_READ_URL``
collapses both back to a single URL — the OSS / pre-split default.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.asyncio


class _SpyTransport(httpx.AsyncBaseTransport):
    """httpx transport that records each request's target URL and
    returns a canned 200 JSON body — no real HTTP, no real upstream."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.requests: list[httpx.Request] = []
        self._body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=self._body, request=request)


def _patch_client_transports(
    client, writer_transport: _SpyTransport, reader_transport: _SpyTransport
) -> None:
    """Swap the client's httpx pools for test spies. Leaves base URL
    calculations untouched so we can assert on the final URL."""
    client._http = httpx.AsyncClient(
        transport=writer_transport, timeout=client._http.timeout
    )
    client._read_http = httpx.AsyncClient(
        transport=reader_transport, timeout=client._http.timeout
    )


async def _fresh_client(writer_url: str, reader_url: str):
    """Build a storage client with the requested URLs patched in, then
    replace its httpx pools with spy transports. Returns (client, writer_spy, reader_spy)."""
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import settings

    with (
        patch.object(settings, "core_storage_api_url", writer_url),
        patch.object(settings, "core_storage_read_url", reader_url),
    ):
        client = CoreStorageClient()
    writer_spy = _SpyTransport()
    reader_spy = _SpyTransport()
    _patch_client_transports(client, writer_spy, reader_spy)
    return client, writer_spy, reader_spy


async def test_get_goes_to_reader_when_read_url_set() -> None:
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.get_memory("11111111-1111-1111-1111-111111111111")
    assert len(reader.requests) == 1
    assert len(writer.requests) == 0
    assert reader.requests[0].url.host == "reader"


async def test_find_by_content_hash_stays_on_writer() -> None:
    """Exact-hash dedup gate on the write path; replica lag would let a
    just-written row slip past dedup."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.find_by_content_hash("t", "abc123")
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_find_duplicate_hash_stays_on_writer() -> None:
    """Update-path dedup check; must see the just-updated row."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.find_duplicate_hash("t", "abc123")
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_get_idempotency_stays_on_writer() -> None:
    """Read-before-write guard for idempotency replay; stale replica would
    let a retried request re-execute instead of returning the cached body."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.get_idempotency("t", "key-1")
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_find_embedding_by_content_hash_stays_on_writer() -> None:
    """Write-path embedding cache lookup. A miss here re-embeds — replica
    lag would cause expensive unnecessary provider calls."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.find_embedding_by_content_hash("t", "abc123")
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_tagged_post_read_goes_to_reader() -> None:
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.scored_search({"tenant_id": "t", "embedding": [], "query": "q"})
    assert len(reader.requests) == 1
    assert len(writer.requests) == 0


async def test_untagged_post_goes_to_writer() -> None:
    """create_memory is an un-tagged POST — writes MUST land on the writer
    even when a reader URL is configured."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.create_memory({"tenant_id": "t", "content": "c"})
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_patch_goes_to_writer() -> None:
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.update_embedding("11111111-1111-1111-1111-111111111111", [0.1])
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_delete_goes_to_writer() -> None:
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.delete_agent("agent-1", tenant_id="t")
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_dedup_lookup_stays_on_writer() -> None:
    """bulk_find_by_content_hashes is used inline during writes; replica
    lag would let a just-written duplicate slip through. Regression
    guard for the CAURA-591 A carve-out."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.bulk_find_by_content_hashes("t", ["hash1", "hash2"])
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_semantic_duplicate_check_stays_on_writer() -> None:
    """find_semantic_duplicate is the near-dup gate on the write path;
    routing to the reader would reintroduce duplicate races."""
    client, writer, reader = await _fresh_client(
        writer_url="http://writer:8002", reader_url="http://reader:8002"
    )
    await client.find_semantic_duplicate({"tenant_id": "t", "embedding": []})
    assert len(writer.requests) == 1
    assert len(reader.requests) == 0


async def test_no_read_url_means_reader_client_is_writer_client() -> None:
    """OSS / pre-split deploy: ``core_storage_read_url=''`` must collapse
    to one pool. Otherwise we'd double the connection budget against the
    same upstream for no reason."""
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import settings

    with (
        patch.object(settings, "core_storage_api_url", "http://only:8002"),
        patch.object(settings, "core_storage_read_url", ""),
    ):
        client = CoreStorageClient()
    assert client._read_http is client._http
    assert client._read_prefix == client._prefix


async def test_read_url_creates_distinct_http_pool() -> None:
    """With the split configured, the two pools must be independent so
    reader throughput can't starve writes (and vice-versa)."""
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import settings

    with (
        patch.object(settings, "core_storage_api_url", "http://writer:8002"),
        patch.object(settings, "core_storage_read_url", "http://reader:8002"),
    ):
        client = CoreStorageClient()
    assert client._read_http is not client._http
    assert client._read_prefix != client._prefix
    await client.close()


async def test_identical_read_and_write_urls_collapse_to_one_pool() -> None:
    """If an operator accidentally sets ``CORE_STORAGE_READ_URL`` to the
    same value as ``CORE_STORAGE_API_URL``, we must not double the
    connection budget against a single upstream."""
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import settings

    with (
        patch.object(settings, "core_storage_api_url", "http://same:8002"),
        patch.object(settings, "core_storage_read_url", "http://same:8002"),
    ):
        client = CoreStorageClient()
    assert client._read_http is client._http
    assert client._read_prefix == client._prefix
    await client.close()


# -------------------------------------------------------------------------
# CAURA-591 Part B Y3 — ID-token Authorization header
# -------------------------------------------------------------------------
#
# When the storage services are deployed with ``--no-allow-unauthenticated``
# core-api must present an identity token for the target's audience. The
# storage client calls ``identity_token.fetch_auth_header(audience)`` on
# every request and attaches the returned dict. Local / test envs have
# no metadata server so the fetch returns ``{}`` and no header is added —
# see ``test_identity_token.py`` for the cache behaviour; the tests here
# assert the header reaches each request method on both pools.


def _patch_fetch(return_value: dict[str, str]):
    """Context manager that patches ``fetch_auth_header`` in both the
    ``identity_token`` module and the reference the storage_client
    holds (it does ``from ... import fetch_auth_header``, which copies
    the reference at import time)."""
    from core_api.clients import identity_token
    from core_api.clients import storage_client as sc_mod

    # Clear module-level state directly — no production test-helper
    # seam required.
    identity_token._cache.clear()
    identity_token._failure_cache.clear()
    identity_token._audience_locks.clear()

    async def _fake(_audience: str) -> dict[str, str]:
        return dict(return_value)  # fresh dict per call to avoid shared-state flakiness

    return patch.multiple(sc_mod, fetch_auth_header=_fake), patch.multiple(
        identity_token, fetch_auth_header=_fake
    )


async def test_authorization_header_attached_on_reader_calls() -> None:
    # https:// audiences exercise the ID-token path; the storage
    # client short-circuits before the metadata fetch for http://
    # audiences (Cloud Run --no-allow-unauthenticated is always TLS,
    # so a plain-HTTP audience is by definition local/in-cluster and
    # never needs a token). These header-propagation tests
    # deliberately use https:// to verify the live token-fetch path.
    a, b = _patch_fetch({"Authorization": "Bearer tok-reader"})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="https://writer:8002", reader_url="https://reader:8002"
        )
        await client.get_memory("11111111-1111-1111-1111-111111111111")
    assert reader.requests[0].headers["Authorization"] == "Bearer tok-reader"


async def test_authorization_header_attached_on_writer_calls() -> None:
    a, b = _patch_fetch({"Authorization": "Bearer tok-writer"})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="https://writer:8002", reader_url="https://reader:8002"
        )
        await client.create_memory({"tenant_id": "t", "content": "c"})
    assert writer.requests[0].headers["Authorization"] == "Bearer tok-writer"


async def test_authorization_header_attached_on_patch() -> None:
    a, b = _patch_fetch({"Authorization": "Bearer tok-writer"})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="https://writer:8002", reader_url="https://reader:8002"
        )
        await client.update_embedding("11111111-1111-1111-1111-111111111111", [0.1])
    assert writer.requests[0].headers["Authorization"] == "Bearer tok-writer"


async def test_authorization_header_attached_on_delete() -> None:
    a, b = _patch_fetch({"Authorization": "Bearer tok-writer"})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="https://writer:8002", reader_url="https://reader:8002"
        )
        await client.delete_agent("agent-1", tenant_id="t")
    assert writer.requests[0].headers["Authorization"] == "Bearer tok-writer"


async def test_http_audience_skips_token_fetch() -> None:
    """Plain-HTTP audience MUST short-circuit before the metadata
    fetch so an unreachable metadata server (local docker, ASGI
    bridges) can't race the call's own timeout budget. The mocked
    ``fetch_auth_header`` returns a header that the storage client
    must NOT attach — proves the bypass is active end-to-end."""
    a, b = _patch_fetch({"Authorization": "Bearer tok-should-not-attach"})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="http://writer:8002", reader_url="http://reader:8002"
        )
        await client.get_memory("11111111-1111-1111-1111-111111111111")
    assert "Authorization" not in reader.requests[0].headers


async def test_no_authorization_header_when_no_credentials() -> None:
    """Environments without a metadata server (tests, local, OSS) get
    ``{}`` from ``fetch_auth_header`` and the storage client must send
    the request without an Authorization header rather than crashing."""
    a, b = _patch_fetch({})
    with a, b:
        client, writer, reader = await _fresh_client(
            writer_url="http://writer:8002", reader_url="http://reader:8002"
        )
        await client.get_memory("11111111-1111-1111-1111-111111111111")
    assert "Authorization" not in reader.requests[0].headers


class _StatusCodeTransport(httpx.AsyncBaseTransport):
    """httpx transport that always returns the given status code. Used
    to verify cache behaviour across 401 vs 403: eviction fires only
    on 401 (token rejected by our identity layer); 403 is a
    permission denial by the target and does NOT imply the token is
    bad, so we keep the cache intact."""

    def __init__(self, status_code: int) -> None:
        self.requests: list[httpx.Request] = []
        self._status = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, content=b"{}", request=request)


async def _run_auth_status_test(
    status_code: int, *, expect_evicted: bool
) -> None:
    """Shared harness — seed a cached token, issue a request that
    gets ``status_code`` back, and assert whether the cache entry
    survived based on ``expect_evicted``."""
    from core_api.clients import identity_token
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import settings

    identity_token._cache.clear()
    identity_token._failure_cache.clear()
    identity_token._audience_locks.clear()

    identity_token._cache["http://writer:8002"] = {"Authorization": "Bearer stale"}
    assert "http://writer:8002" in identity_token._cache

    with (
        patch.object(settings, "core_storage_api_url", "http://writer:8002"),
        patch.object(settings, "core_storage_read_url", "http://reader:8002"),
    ):
        client = CoreStorageClient()

    client._http = httpx.AsyncClient(
        transport=_StatusCodeTransport(status_code),
        timeout=client._http.timeout,
    )
    client._read_http = httpx.AsyncClient(
        transport=_SpyTransport(), timeout=client._read_http.timeout
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.create_memory({"tenant_id": "t", "content": "c"})

    if expect_evicted:
        assert "http://writer:8002" not in identity_token._cache
    else:
        assert "http://writer:8002" in identity_token._cache


async def test_401_response_evicts_cached_id_token() -> None:
    """401 = token explicitly rejected at the identity layer. The
    cache entry must go so the next request forces a fresh fetch —
    otherwise every request 401s for 50 min."""
    await _run_auth_status_test(401, expect_evicted=True)


async def test_403_response_keeps_cached_id_token() -> None:
    """403 = permission denied by the target, not a bad token. The
    cache must survive so we don't thrash the metadata server with
    refreshes of a token that was authoritatively authed — the real
    fix is IAM-side (grant the caller's SA the right role)."""
    await _run_auth_status_test(403, expect_evicted=False)
