"""Tests for core-worker's CAURA-591 Y3 ID-token helper.

Mirrors ``tests/test_identity_token.py`` for core-api: same caching
semantics, same per-audience locking, same eviction behaviour.
The worker has its own copy of ``identity_token.py`` (rather than
factoring out a ``common/`` shared module) because the cache is
process-local module state — sharing the file but not the state
costs us a per-service refactor for no benefit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


def _reset_caches() -> None:
    """Clear module-level caches + lock dict directly — no production
    test-helper seam needed."""
    from core_worker.clients import identity_token

    identity_token._cache.clear()
    identity_token._failure_cache.clear()
    identity_token._audience_locks.clear()


async def test_returns_empty_header_when_no_credentials() -> None:
    """Permanent no-creds env (google.auth unimportable) — cached at
    the full TTL so we don't reimport on every request."""
    from core_worker.clients import identity_token

    _reset_caches()
    with patch.object(identity_token, "_fetch_blocking", lambda _aud: identity_token._NO_CREDS):
        header = await identity_token.fetch_auth_header("https://example.run.app")
    assert header == {}
    # The empty dict is cached, so a second call should hit the cache
    # without re-invoking _fetch_blocking.
    call_count = 0

    def _record(_aud):
        nonlocal call_count
        call_count += 1
        return identity_token._NO_CREDS

    with patch.object(identity_token, "_fetch_blocking", _record):
        await identity_token.fetch_auth_header("https://example.run.app")
    assert call_count == 0, "permanent no-creds should cache the empty dict"


async def test_caches_token_across_calls() -> None:
    from core_worker.clients import identity_token

    _reset_caches()
    call_count = 0

    def _fake_fetch(audience: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"token-for-{audience}"

    with patch.object(identity_token, "_fetch_blocking", _fake_fetch):
        h1 = await identity_token.fetch_auth_header("https://svc.run.app")
        h2 = await identity_token.fetch_auth_header("https://svc.run.app")
    assert h1 == h2 == {"Authorization": "Bearer token-for-https://svc.run.app"}
    assert call_count == 1


async def test_same_dict_returned_across_calls() -> None:
    """The cache stores the pre-built header dict; successive calls
    must return the SAME object so we don't pay the allocation +
    f-string cost per request on the hot path."""
    from core_worker.clients import identity_token

    _reset_caches()
    with patch.object(identity_token, "_fetch_blocking", lambda _aud: "tok"):
        h1 = await identity_token.fetch_auth_header("https://svc.run.app")
        h2 = await identity_token.fetch_auth_header("https://svc.run.app")
    assert h1 is h2


async def test_transient_failure_does_not_lock_out_auth() -> None:
    """A fetch exception must NOT be cached at the 50-min TTL —
    otherwise a brief startup hiccup would disable auth for an hour.
    Only the 30-s failure cooldown holds."""
    from core_worker.clients import identity_token

    _reset_caches()

    calls: list[str] = []

    def _fail_once_then_succeed(audience: str) -> str:
        calls.append("call")
        if len(calls) == 1:
            raise RuntimeError("transient metadata-server blip")
        return "recovered"

    with patch.object(identity_token, "_fetch_blocking", _fail_once_then_succeed):
        h1 = await identity_token.fetch_auth_header("https://svc.run.app")
        assert h1 == {}
        # Within the 30 s cooldown we continue to return {} without retry.
        h2 = await identity_token.fetch_auth_header("https://svc.run.app")
        assert h2 == {}
        # Clear the short cooldown and retry — must succeed this time.
        identity_token._failure_cache.clear()
        h3 = await identity_token.fetch_auth_header("https://svc.run.app")
    assert h3 == {"Authorization": "Bearer recovered"}


async def test_evict_drops_cached_entries() -> None:
    """Called on 401 from storage client; next fetch must re-hit the
    metadata server rather than return the stale cached token."""
    from core_worker.clients import identity_token

    _reset_caches()
    call_count = 0

    def _fake(_aud: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"token-{call_count}"

    with patch.object(identity_token, "_fetch_blocking", _fake):
        await identity_token.fetch_auth_header("https://svc.run.app")
        identity_token.evict("https://svc.run.app")
        await identity_token.fetch_auth_header("https://svc.run.app")
    assert call_count == 2


async def test_evict_is_idempotent_on_unknown_audience() -> None:
    from core_worker.clients import identity_token

    _reset_caches()
    # No cache entry for this audience — evict must not raise.
    identity_token.evict("https://never-cached.run.app")
