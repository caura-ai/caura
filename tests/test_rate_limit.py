"""Tests for CAURA-603 part B — slowapi rate limiter.

Exercises the limiter at the HTTP layer (decorators applied to
/memories, /memories/bulk, /search). The in-memory slowapi backend is
used here so tests don't need Redis; prod swaps to Redis via
``settings.redis_url``.

Burst tests fire requests via ``asyncio.gather`` rather than a
sequential ``for`` loop. The limiter's window is wall-clock; a
sequential loop's per-request roundtrip stretches the burst past the
1-second window on slower runners and lets every request pass under
budget. Concurrent fire is what an actual abusive client looks like
anyway, and it makes the test runner-speed-independent.
"""

import asyncio
from types import SimpleNamespace

import pytest

from core_api.middleware.rate_limit import _key_func, limiter

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_limiter_for_rate_tests():
    """Overrides the session-scoped conftest fixture that disables the
    limiter for the rest of the suite. Clears buckets between tests so
    earlier bursts don't leak state into later tests."""
    prev = limiter.enabled
    limiter.enabled = True
    yield
    limiter.enabled = prev
    limiter.reset()


# ── key_func ──


class _FakeRequest:
    def __init__(self, headers: dict[str, str], client_host: str = "127.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": client_host})()
        # slowapi's get_remote_address looks at scope too; emulate it.
        self.scope = {"client": (client_host, 0)}
        # _key_func seeds request.state.view_rate_limit (the fail-open default);
        # a real Starlette request always has .state.
        self.state = SimpleNamespace()


async def test_key_func_prefers_api_key_over_ip():
    # API-key path produces a `key:<32-hex>` token, not the raw key.
    req = _FakeRequest(headers={"x-api-key": "mc_abcdef12345678_secret"})
    out = _key_func(req)
    assert out.startswith("key:")
    assert len(out) == len("key:") + 32
    # Secret must NOT appear verbatim — it's hashed.
    assert "mc_abcdef" not in out


async def test_key_func_accepts_bearer_auth():
    req = _FakeRequest(headers={"authorization": "Bearer mc_bearertoken_abcde"})
    out = _key_func(req)
    assert out.startswith("key:")
    assert "bearertoken" not in out


async def test_key_func_different_keys_yield_different_buckets():
    # Keys sharing a 16-char prefix used to collide; hashing fixes that.
    a = _FakeRequest(headers={"x-api-key": "mc_samentenantid_A"})
    b = _FakeRequest(headers={"x-api-key": "mc_samentenantid_B"})
    assert _key_func(a) != _key_func(b)


async def test_key_func_falls_back_to_ip_without_api_key():
    req = _FakeRequest(headers={}, client_host="198.51.100.7")
    assert _key_func(req) == "ip:198.51.100.7"


async def test_key_func_seeds_fail_open_default_without_clobbering():
    """key_func seeds request.state.view_rate_limit=None (fail-open default) on
    first call, but — since slowapi calls it once per applied limit — must NOT
    reset a value a prior limit's hit() already wrote (else multi-limit routes
    drop X-RateLimit headers on a partial Redis outage)."""
    req = _FakeRequest(headers={"x-api-key": "mc_multi_limit_probe"})
    # First call (before any hit) seeds the fail-open default.
    _key_func(req)
    assert req.state.view_rate_limit is None
    # Simulate an earlier limit's successful hit() setting the real value.
    req.state.view_rate_limit = ("limit-1", ["result"])
    # A later key_func call (second stacked limit) must leave it intact.
    _key_func(req)
    assert req.state.view_rate_limit == ("limit-1", ["result"])


async def test_inject_headers_is_noop_for_none_limit():
    """The fail-open path relies on slowapi's ``_inject_headers`` being a no-op
    when ``current_limit`` is None (slowapi 0.1.10 extension.py:
    ``if ... and current_limit is not None``). Pin that assumption at CI so a
    slowapi upgrade that drops the guard fails here, not by 500ing the
    swallowed-error path in prod. ``headers_enabled=True`` so the None guard is
    the operative one (not the earlier headers-disabled short-circuit)."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from starlette.responses import Response

    lim = Limiter(key_func=get_remote_address, headers_enabled=True)
    lim.enabled = True
    resp = Response()
    # Must return the response unchanged and must not raise on a None limit.
    assert lim._inject_headers(resp, None) is resp


# ── HTTP layer: 429 on burst ──


async def _burst(coro_factories, *, concurrency: int) -> list[int]:
    """Fire a batch of requests with bounded concurrency, return status codes.

    Why bounded: slowapi's decorator runs INSIDE the wrapped route, so
    FastAPI resolves dependencies (auth, ``Depends(get_db)``) BEFORE the
    limiter fires. An unbounded ``asyncio.gather(*[100 reqs])`` opens 100
    Postgres connections simultaneously and exhausts the test pool with
    "sorry, too many clients already" — masking the 429s the test exists
    to assert. ``concurrency`` chosen to comfortably exceed the per-second
    budget (so 429s fire) while staying under the DB pool ceiling.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(make):
        async with sem:
            r = await make()
            return r.status_code

    return await asyncio.gather(*[_one(f) for f in coro_factories])


async def test_search_rate_limit_returns_429_after_budget(client):
    """The default 30/second limit should trip within a 100-request burst
    from a single API key. The burst is capped at 50 in-flight via a
    semaphore so the DB connection pool stays intact while still
    delivering > 30 requests inside one rate-limit window."""
    headers = {"x-api-key": "mc_rate_test_search_key"}
    body = {"tenant_id": "default", "query": "hello"}
    codes = await _burst(
        [lambda: client.post("/api/v1/search", json=body, headers=headers)] * 100,
        concurrency=50,
    )
    assert 429 in codes, f"expected at least one 429 among {codes}"


async def test_write_rate_limit_returns_429_after_budget(client):
    """Writes have a stricter limit (10/second by default) — a 50-request
    bounded burst from the same key should reliably see a 429.

    Each request carries a unique ``content`` to avoid the dedup 409 path
    masking a 429."""
    headers = {"x-api-key": "mc_rate_test_write_key"}
    base_body = {
        "tenant_id": "default",
        "agent_id": "rate-test-agent",
        "memory_type": "fact",
    }
    factories = [
        (
            lambda i=i: client.post(
                "/api/v1/memories",
                json={**base_body, "content": f"rate-limit probe {i}"},
                headers=headers,
            )
        )
        for i in range(50)
    ]
    codes = await _burst(factories, concurrency=25)
    assert 429 in codes, f"expected at least one 429 among {codes}"


async def test_health_is_not_rate_limited(client):
    """/health runs pre-auth and must not be rate-limited or the deploy
    gate (CAURA-603 part A) stops working under load."""
    codes = []
    for _ in range(60):
        resp = await client.get("/api/v1/health")
        codes.append(resp.status_code)
    assert 429 not in codes


# ── fail-open: rate-limit storage (Redis) outage must not 500 ──


async def test_search_fails_open_when_rate_limit_storage_errors(client, monkeypatch):
    """A Redis/storage outage must FAIL OPEN, not 500. With ``swallow_errors=True``
    slowapi swallows the backend error ("Failed to rate limit. Swallowing error"),
    but its limit decorator still reads ``request.state.view_rate_limit`` on the way
    out — which is unset on the swallowed path. Without ``_key_func``'s ``None``
    seed that's an ``AttributeError`` → 500 (prod 2026-06-17 on /api/v1/search when
    Redis connection reset). The request must be served normally, not 500'd."""

    def _boom(*args, **kwargs):
        raise ConnectionError("simulated Redis down")

    # slowapi calls ``limiter.limiter.hit(...)`` against the backend during the
    # per-route check; make that raise to emulate Redis being unreachable.
    monkeypatch.setattr(limiter.limiter, "hit", _boom)

    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": "default", "query": "hello"},
        headers={"x-api-key": "mc_failopen_probe"},
    )

    assert resp.status_code == 200, (
        f"rate-limit storage outage must fail open, got {resp.text}"
    )
