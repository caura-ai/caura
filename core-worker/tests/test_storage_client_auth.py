"""Tests for CAURA-591 Y3 / CAURA-595: storage_client signs every
request with a Cloud Run ID token bound to the writer service URL,
evicts the cached token on 401, and retries once with a fresh token
so a credential rotation is invisible to callers.

Auth is wired through one private helper, ``_signed_call`` — these
tests exercise it indirectly via the public endpoint helpers
(``find_embedding_by_content_hash``, ``update_memory_embedding``,
``update_memory_enrichment``) so a future endpoint helper can't drift
silently around the signing path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from core_worker.clients import identity_token, storage_client

pytestmark = pytest.mark.asyncio


def _reset_module_state() -> None:
    """Clear the singleton + audience + identity-token caches so each
    test starts with the same blank slate. The singleton is module-
    global, so leaks between tests would silently bind one test's
    audience to the next test's HTTP mock."""
    storage_client._client = None
    storage_client._audience = None
    identity_token._cache.clear()
    identity_token._failure_cache.clear()
    identity_token._audience_locks.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_module_state()
    yield
    _reset_module_state()


def _seed_audience(audience: str = "https://writer.run.app") -> None:
    """Set ``_audience`` directly without spinning up a real httpx
    pool — we don't need the singleton for these tests, just the
    audience binding the auth header against."""
    storage_client._audience = audience


def _seed_token(audience: str, token: str) -> None:
    """Pre-populate the identity-token cache so signing returns a
    deterministic header without touching ``_fetch_blocking``."""
    identity_token._cache[audience] = {"Authorization": f"Bearer {token}"}


def _ok_response() -> MagicMock:
    """``raise_for_status`` is a no-op so the public helpers complete
    cleanly on 200 — we're testing the request side, not response
    handling here."""
    resp = MagicMock(status_code=200)
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


# ── Audience unset (OSS / docker-compose / default unit-test path) ───


async def test_unauthenticated_when_audience_unset() -> None:
    """``get_storage_client()`` not called yet → no audience to sign
    against; the request goes out without an Authorization header
    so the docker-compose ``allUsers`` writer continues to accept it."""
    assert storage_client._audience is None

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=_ok_response())

    await storage_client.update_memory_embedding(
        client,
        memory_id=uuid4(),
        tenant_id="tenant-A",
        embedding=[0.1] * 8,
    )

    headers = client.patch.await_args.kwargs["headers"]
    assert "Authorization" not in headers


# ── update_memory_embedding signs requests + evicts on 401 ───────────


async def test_update_memory_embedding_sends_auth_header() -> None:
    _seed_audience()
    _seed_token("https://writer.run.app", "tok-embed")

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=_ok_response())

    await storage_client.update_memory_embedding(
        client,
        memory_id=uuid4(),
        tenant_id="tenant-A",
        embedding=[0.1] * 8,
    )

    headers = client.patch.await_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer tok-embed"}


async def test_401_recovers_via_one_shot_retry_with_fresh_token() -> None:
    """A mid-TTL credential rotation must be invisible to callers:
    on 401, evict the stale token, re-fetch, and retry the request
    once with the new token. Without this retry, every in-flight task
    would burn one Pub/Sub delivery attempt during a rotation."""
    _seed_audience()
    _seed_token("https://writer.run.app", "stale")

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(side_effect=[MagicMock(status_code=401), _ok_response()])

    # ``_fetch_blocking`` is the sync metadata-server entry — patch it
    # so the post-eviction re-fetch returns the rotated token.
    with patch.object(identity_token, "_fetch_blocking", lambda _aud: "fresh"):
        await storage_client.update_memory_embedding(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            embedding=[0.1] * 8,
        )

    # Two PATCHes total: the 401, then the retry with the fresh token.
    assert client.patch.await_count == 2
    first_headers = client.patch.await_args_list[0].kwargs["headers"]
    second_headers = client.patch.await_args_list[1].kwargs["headers"]
    assert first_headers == {"Authorization": "Bearer stale"}
    assert second_headers == {"Authorization": "Bearer fresh"}
    # Cache holds the rotated token, ready for the next request.
    assert identity_token._cache["https://writer.run.app"] == {"Authorization": "Bearer fresh"}


async def test_persistent_401_propagates_after_retry() -> None:
    """If the rotation didn't help (or the SA was actually unbound),
    the second 401 propagates to the caller's normal error path —
    consumer nacks → Pub/Sub redelivers. The cache is left empty so
    the NEXT delivery attempt re-fetches from scratch rather than
    reusing the just-rejected token."""
    _seed_audience()
    _seed_token("https://writer.run.app", "stale")

    response = MagicMock(status_code=401)
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("unauth", request=MagicMock(), response=response)
    )
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)

    with patch.object(identity_token, "_fetch_blocking", lambda _aud: "still-bad"):
        with pytest.raises(httpx.HTTPStatusError):
            await storage_client.update_memory_embedding(
                client,
                memory_id=uuid4(),
                tenant_id="tenant-A",
                embedding=[0.1] * 8,
            )

    # Two PATCHes: original + one retry, then propagate.
    assert client.patch.await_count == 2


async def test_update_memory_embedding_does_not_evict_on_403() -> None:
    """403 = token accepted but caller lacks IAM permission. The
    token is fine; eviction won't fix the IAM mismatch and would
    cost an unnecessary metadata-server round trip on every request."""
    _seed_audience()
    _seed_token("https://writer.run.app", "fine")

    response = MagicMock(status_code=403)
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("forbidden", request=MagicMock(), response=response)
    )
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await storage_client.update_memory_embedding(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            embedding=[0.1] * 8,
        )

    assert "https://writer.run.app" in identity_token._cache


# ── update_memory_enrichment signs requests ──────────────────────────


async def test_update_memory_enrichment_sends_auth_header() -> None:
    _seed_audience()
    _seed_token("https://writer.run.app", "tok-enrich")

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=_ok_response())

    await storage_client.update_memory_enrichment(
        client,
        memory_id=uuid4(),
        tenant_id="tenant-A",
        fields={"memory_type": "fact", "weight": 0.7},
    )

    headers = client.patch.await_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer tok-enrich"}


async def test_update_memory_enrichment_skips_empty_patch_without_signing() -> None:
    """Empty ``fields`` → no HTTP call, no token fetch. The early-return
    path costs nothing."""
    _seed_audience()
    # Deliberately do NOT seed a token — if the helper tried to sign,
    # it'd hit the (mocked-out-by-default) metadata server and stall.

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock()

    await storage_client.update_memory_enrichment(
        client,
        memory_id=uuid4(),
        tenant_id="tenant-A",
        fields={},
    )

    client.patch.assert_not_called()


# ── find_embedding_by_content_hash signs requests ────────────────────


async def test_find_embedding_by_content_hash_sends_auth_header() -> None:
    """Storage returns the bare ``list[float]`` per its contract — not
    a wrapper dict. Treating it as a dict would crash the helper at
    ``.get("embedding")``."""
    _seed_audience()
    _seed_token("https://writer.run.app", "tok-cache")

    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value=[0.1] * 8)
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response)

    result = await storage_client.find_embedding_by_content_hash(
        client,
        tenant_id="tenant-A",
        content_hash="hash-1",
    )

    assert result == [0.1] * 8
    headers = client.get.await_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer tok-cache"}


async def test_find_embedding_by_content_hash_returns_none_on_null_body() -> None:
    """JSON-null body (no cached row for this content_hash) → return
    ``None``. Pre-fix this raised ``AttributeError: 'NoneType' object
    has no attribute 'get'`` because the helper called ``.get()`` on
    a parsed-null body. Surfaced once the CAURA-595 ID-token PR
    landed and storage calls actually started succeeding past 403."""
    _seed_audience()
    _seed_token("https://writer.run.app", "tok-cache")

    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value=None)
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response)

    result = await storage_client.find_embedding_by_content_hash(
        client,
        tenant_id="tenant-A",
        content_hash="hash-1",
    )

    assert result is None


async def test_find_embedding_by_content_hash_returns_none_on_bad_json() -> None:
    """A 200 with a malformed body (stray proxy partial response, an
    HTML error page sneaking through, …) ``raise``s out of
    ``resp.json()`` — the helper must catch it and return ``None``
    rather than propagate to the consumer (which would nack and force
    a redelivery loop the cache lookup is allowed to skip)."""
    _seed_audience()
    _seed_token("https://writer.run.app", "tok-cache")

    response = MagicMock(status_code=200)
    response.json = MagicMock(side_effect=ValueError("not json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response)

    result = await storage_client.find_embedding_by_content_hash(
        client,
        tenant_id="tenant-A",
        content_hash="hash-1",
    )

    assert result is None


# ── get_storage_client wires audience from settings ──────────────────


async def test_get_storage_client_strips_trailing_slash_for_audience(monkeypatch) -> None:
    """A trailing slash in ``CORE_STORAGE_API_URL`` would produce a
    different audience claim than Cloud Run validates against the
    canonical service URL — strip it before binding."""
    monkeypatch.setenv("CORE_STORAGE_API_URL", "https://writer.run.app/")

    client = storage_client.get_storage_client()
    try:
        assert storage_client._audience == "https://writer.run.app"
    finally:
        await client.aclose()


async def test_close_storage_client_clears_audience() -> None:
    """Lifespan shutdown must reset both the singleton AND the audience —
    otherwise a re-init in the same process picks up a stale audience
    bound to the previous test's settings."""
    storage_client._audience = "https://stale.run.app"
    storage_client._client = MagicMock(spec=httpx.AsyncClient)
    storage_client._client.aclose = AsyncMock()

    await storage_client.close_storage_client()

    assert storage_client._client is None
    assert storage_client._audience is None
