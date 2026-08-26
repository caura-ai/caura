"""D14 — 429s (and limited routes generally) must carry back-off headers.

slowapi's ``_inject_headers`` silently no-ops unless the Limiter was built
with ``headers_enabled=True`` — which it never was, so no response carried
``X-RateLimit-*`` and no 429 carried ``Retry-After``, even though the custom
429 handler in app.py explicitly re-runs the injector. Hermes experienced
this as "MCP feels fragile": throttling with no back-off signal.
"""

import pytest
from fastapi.responses import JSONResponse

from core_api.middleware.rate_limit import limiter

pytestmark = pytest.mark.unit


def test_limiter_headers_enabled():
    # The whole fix: without this flag the injector below is a no-op.
    # (``limiter.enabled`` is False under test settings — rate limiting is
    # off in tests — so assert the header flag, which is what shipped.)
    assert getattr(limiter, "_headers_enabled", False) is True


def test_inject_headers_actually_injects(monkeypatch):
    """Drive slowapi's injector directly with a synthetic window and assert
    the headers land — the exact call app.py's 429 handler makes.
    ``enabled`` is forced on for the assertion since test settings disable
    limiting; production runs with it enabled."""
    import time

    monkeypatch.setattr(limiter, "enabled", True)

    from limits import RateLimitItemPerSecond

    item = RateLimitItemPerSecond(10)
    reset_at = int(time.time()) + 1
    response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    out = limiter._inject_headers(response, (item, [str(reset_at)]))
    assert out.headers.get("X-RateLimit-Limit") == "10"
    # Remaining comes from live window stats (fresh storage here) — assert
    # presence, not a specific count.
    assert "X-RateLimit-Remaining" in out.headers
    assert "X-RateLimit-Reset" in out.headers or "Retry-After" in out.headers


async def test_bulkhead_429_carries_retry_after(monkeypatch):
    """The per-tenant concurrency bulkhead's 429 said "retry shortly" with no
    Retry-After — the third 429 source (after nginx + slowapi) the D14 wet
    test surfaced. FastAPI turns HTTPException.headers into response headers."""
    import asyncio

    from fastapi import HTTPException

    from core_api.middleware import per_tenant_concurrency as ptc

    class _NeverAcquires:
        async def acquire(self):
            await asyncio.sleep(3600)

        def release(self):
            pass

    monkeypatch.setattr(ptc, "_get_semaphore", lambda scope, tenant_id: _NeverAcquires())
    monkeypatch.setattr(ptc.settings, "per_tenant_acquire_timeout_seconds", 0.01)
    with pytest.raises(HTTPException) as exc_info:
        async with ptc.per_tenant_slot("search", "t1"):
            pass
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers.get("Retry-After") == "1"
