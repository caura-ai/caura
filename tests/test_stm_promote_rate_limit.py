"""H-17 residual / M-35 — ``POST /stm/promote`` pays the write rate limit and
the per-tenant write slot, like ``POST /memories``.

The 2026-08-14 audit (H-17) found the STM door into long-term memory cheaper
than the front door. #804 gave promote every authz and metering gate the
memories route has, but two traffic gates stayed missing (2026-09-02 M-35):
``@write_limit`` (per-key rate limit) and ``per_tenant_slot("write", …)``
(per-tenant in-flight cap). A client throttled on ``POST /memories`` could
keep writing at full speed through ``/stm/promote``.

Same harness as ``test_stm_admin_tenant.py``: the STM router alone, the auth
dependency overridden, and the service layer patched at the module boundary —
no DB, no Redis. The limiter is re-enabled locally (conftest disables it
suite-wide) on an in-memory store, and the app carries the slowapi state +
handler the way ``core_api.app`` wires them, because the decorator reads both
from ``app.state`` at request time.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings
from core_api.middleware import per_tenant_concurrency
from core_api.middleware.rate_limit import limiter
from core_api.routes import stm

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-acme"
AGENT = "agent-1"
PROMOTE_BODY = {"agent_id": AGENT, "content": "a durable note"}


@pytest.fixture(autouse=True)
def _stm_on_and_seams(monkeypatch):
    monkeypatch.setattr(settings, "use_stm", True)

    class _Config:
        require_agent_approval = False

    async def _resolve_config(tenant_id):
        return _Config()

    async def _resolve_write_agent(chosen_agent_id, tenant_id, fleet_id, **kwargs):
        return {"trust_level": 2, "fleet_id": fleet_id}, chosen_agent_id

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(stm, "resolve_write_agent", _resolve_write_agent)
    monkeypatch.setattr(stm, "enforce_fleet_write", _noop)
    monkeypatch.setattr(stm, "check_and_increment", _noop)


@pytest.fixture
def promote_seam(monkeypatch):
    """The LTM write itself. Returns a holder whose ``gate`` (when set) makes
    every promote block until released, so slots can be held open on demand."""

    class _Seam:
        gate: asyncio.Event | None = None
        calls = 0

    seam = _Seam()

    async def _promote(**kwargs):
        seam.calls += 1
        if seam.gate is not None:
            await seam.gate.wait()
        return {"id": "m-1"}

    monkeypatch.setattr("core_api.services.stm_service.promote", _promote)
    return seam


@pytest.fixture
def limiter_on():
    prev = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.enabled = prev
    limiter.reset()


@pytest.fixture(autouse=True)
def _fresh_semaphores():
    per_tenant_concurrency._reset_for_tests()
    yield
    per_tenant_concurrency._reset_for_tests()


def make_client() -> AsyncClient:
    app = FastAPI()
    app.include_router(stm.router, prefix="/api/v1")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    auth = AuthContext(tenant_id=TENANT, agent_id=AGENT, readable_tenant_ids=[TENANT])

    async def _auth_dep():
        return auth

    app.dependency_overrides[get_auth_context] = _auth_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_promote_success_carries_rate_limit_headers(limiter_on, promote_seam):
    """D14 contract, and the regression a missing ``response`` param would
    cause: with ``headers_enabled=True`` slowapi injects into the declared
    ``Response`` on every success, and 500s the route when there is none."""
    async with make_client() as client:
        r = await client.post(
            "/api/v1/stm/promote",
            params={"tenant_id": TENANT},
            json=PROMOTE_BODY,
            headers={"x-api-key": "mc_stm_promote_hdr_probe"},
        )
    assert r.status_code == 200, r.text
    assert "x-ratelimit-limit" in r.headers, dict(r.headers)
    assert promote_seam.calls == 1


async def test_promote_returns_429_after_write_budget(limiter_on, promote_seam):
    """Same key, one window, more requests than ``RATE_LIMIT_WRITE`` allows —
    the burst must see a 429, as it does on ``POST /memories``."""
    headers = {"x-api-key": "mc_stm_promote_burst_key"}
    async with make_client() as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/api/v1/stm/promote",
                    params={"tenant_id": TENANT},
                    json={**PROMOTE_BODY, "content": f"burst {i}"},
                    headers=headers,
                )
                for i in range(50)
            ]
        )
    codes = [r.status_code for r in responses]
    assert 429 in codes, f"expected at least one 429 among {codes}"
    assert 200 in codes, f"the budget itself must still be honoured: {codes}"


async def test_promote_holds_a_per_tenant_write_slot(monkeypatch, promote_seam):
    """With the tenant's write slots all held by in-flight promotes, the next
    one fails fast with 429 + Retry-After instead of queueing."""
    monkeypatch.setattr(settings, "per_tenant_write_concurrency", 1)
    monkeypatch.setattr(settings, "per_tenant_acquire_timeout_seconds", 0.05)
    per_tenant_concurrency._reset_for_tests()

    promote_seam.gate = asyncio.Event()
    async with make_client() as client:
        first = asyncio.create_task(
            client.post(
                "/api/v1/stm/promote", params={"tenant_id": TENANT}, json=PROMOTE_BODY
            )
        )
        # Let the first request reach the write and park on the gate.
        for _ in range(50):
            if promote_seam.calls:
                break
            await asyncio.sleep(0.01)
        assert promote_seam.calls == 1

        second = await client.post(
            "/api/v1/stm/promote", params={"tenant_id": TENANT}, json=PROMOTE_BODY
        )
        assert second.status_code == 429, second.text
        assert second.headers.get("retry-after") == "1"
        assert "concurrent write" in second.text

        promote_seam.gate.set()
        assert (await first).status_code == 200


async def test_a_refused_promote_does_not_consume_a_slot(monkeypatch, promote_seam):
    """The slot wraps the write only. A request that fails a policy gate
    (here: a server-reserved memory type) must not hold or burn a slot."""
    monkeypatch.setattr(settings, "per_tenant_write_concurrency", 1)
    monkeypatch.setattr(settings, "per_tenant_acquire_timeout_seconds", 0.05)
    per_tenant_concurrency._reset_for_tests()

    async with make_client() as client:
        refused = await client.post(
            "/api/v1/stm/promote",
            params={"tenant_id": TENANT},
            json={**PROMOTE_BODY, "memory_type": "rule"},
        )
        assert refused.status_code == 422, refused.text
        ok = await client.post(
            "/api/v1/stm/promote", params={"tenant_id": TENANT}, json=PROMOTE_BODY
        )
    assert ok.status_code == 200, ok.text
    assert promote_seam.calls == 1
