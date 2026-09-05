"""Tests for CAURA-603 — /health deploy gate.

The endpoint must return 503 when any required dependency is down so
Cloud Run deploy gates and probes can fail-fast on the status code.
Required deps depend on configuration: storage is always required;
Redis is required only when ``settings.redis_url`` is set.
"""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ── Happy path ──


async def test_health_ok_when_storage_connected(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["storage"] == "connected"


# ── Storage failure → 503 ──


async def test_health_503_when_storage_down(client):
    with patch(
        "core_api.clients.storage_client.CoreStorageClient.count_all",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "unhealthy"
    assert "storage" in data["unhealthy_dependencies"]
    # Fixed opaque string — raw exception messages can leak URLs/creds.
    assert data["storage"] == "unreachable"


# ── Redis gating depends on settings.redis_url ──


async def test_health_200_when_redis_not_configured(client):
    # Default test config leaves redis_url empty — Redis is optional;
    # absence is not a failure.
    resp = await client.get("/api/v1/health")
    data = resp.json()
    assert resp.status_code == 200
    assert data["redis"] == "not configured"
    assert data["status"] == "ok"


async def test_health_503_when_redis_required_and_down(client):
    from core_api.config import settings

    with (
        patch.object(settings, "redis_url", "redis://ghost:6379/0"),
        patch("core_api.routes.health.redis_healthy", AsyncMock(return_value=False)),
    ):
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "unhealthy"
    assert "redis" in data["unhealthy_dependencies"]
    assert data["redis"] == "unavailable"


async def test_health_200_when_redis_required_and_up(client):
    from core_api.config import settings

    with (
        patch.object(settings, "redis_url", "redis://phantom:6379/0"),
        patch("core_api.routes.health.redis_healthy", AsyncMock(return_value=True)),
    ):
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["redis"] == "connected"
    assert data["status"] == "ok"


# ── Event bus (CAURA-593 follow-up) ──


async def test_health_200_reports_event_bus_ok_by_default(client):
    """InProcessEventBus is the OSS default and always reports healthy."""
    resp = await client.get("/api/v1/health")
    data = resp.json()
    assert resp.status_code == 200
    assert data["event_bus"] == "ok"


async def test_health_503_when_event_bus_unhealthy(client):
    """A stubbed bus with ``is_healthy=False`` flips status to 503. Covers
    the Pub/Sub-pull-loop-halted case without depending on the SDK."""
    from types import SimpleNamespace

    with patch(
        "core_api.routes.health.get_event_bus",
        return_value=SimpleNamespace(is_healthy=False),
    ):
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "unhealthy"
    assert data["event_bus"] == "unhealthy"
    assert "event_bus" in data["unhealthy_dependencies"]


async def test_health_503_when_event_bus_factory_raises(client):
    """A RuntimeError from get_event_bus() (missing Pub/Sub env vars)
    must surface as a structured 503, not a bare 500. Regression guard
    for the review finding where the probe lacked try/except."""
    with patch(
        "core_api.routes.health.get_event_bus",
        side_effect=RuntimeError("EVENT_BUS_BACKEND=pubsub requires GCP_PROJECT_ID"),
    ):
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "unhealthy"
    assert data["event_bus"] == "error"
    assert "event_bus" in data["unhealthy_dependencies"]


# ── Probe budget vs. the storage client's own retry policy ──


async def test_probe_timeout_clears_storage_connect_ceiling():
    """``PROBE_TIMEOUT_SECONDS`` must outlive ONE storage connect attempt.

    Asserted as a RELATION against the client's own configuration, never
    against the literal ``12.0``. Both numbers are legitimately tunable; the
    property that has to survive any retune is that the probe does not give
    up before the transport it is measuring has finished a single attempt.

    This was ``5.0 > 5.0`` → False. Equal budgets meant the probe could
    absorb none of the five connect retries ``CONNECT_PHASE_MAX_ATTEMPTS``
    grants, so one slow connection setup reported ``storage: unreachable``
    while storage was in fact answering the very same call in ~30ms.
    """
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.constants import (
        PROBE_TIMEOUT_SECONDS,
        STORAGE_CONNECT_TIMEOUT_SECONDS,
    )

    assert STORAGE_CONNECT_TIMEOUT_SECONDS < PROBE_TIMEOUT_SECONDS, (
        f"probe budget {PROBE_TIMEOUT_SECONDS}s must exceed the storage client's "
        f"per-attempt connect ceiling {STORAGE_CONNECT_TIMEOUT_SECONDS}s, or the probe "
        f"times out first and reports a healthy dependency as unreachable"
    )

    # The constant is only meaningful if the pool actually uses it — otherwise
    # the ordering above guards a number nothing reads. Assert the wiring, not
    # a second copy of the value.
    pool = CoreStorageClient._make_pool()
    try:
        assert pool.timeout.connect == STORAGE_CONNECT_TIMEOUT_SECONDS
    finally:
        await pool.aclose()


async def test_dependency_probes_run_concurrently():
    """Storage and redis must be in flight together, not one after the other.

    Concurrency is what keeps the wall-clock ceiling at ONE
    ``PROBE_TIMEOUT_SECONDS`` after that budget was raised; run sequentially
    they add up. Asserted by observing overlap rather than by timing the
    call — a wall-clock threshold measures runner contention, not the
    implementation (see the benchmark note in ``pytest.ini``).
    """
    import asyncio

    from core_api.config import settings
    from core_api.routes.health import _probe_dependencies

    in_flight = 0
    max_in_flight = 0

    async def _enter_and_yield():
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)  # yield so a sibling probe can start
        in_flight -= 1

    async def _slow_count(*_args, **_kwargs):
        await _enter_and_yield()
        return 0

    async def _slow_redis():
        await _enter_and_yield()
        return True

    with (
        patch.object(settings, "redis_url", "redis://phantom:6379/0"),
        patch(
            "core_api.clients.storage_client.CoreStorageClient.count_all",
            AsyncMock(side_effect=_slow_count),
        ),
        patch("core_api.routes.health.redis_healthy", _slow_redis),
    ):
        deps, unhealthy = await _probe_dependencies()

    assert max_in_flight == 2, (
        f"expected storage and redis probes to overlap, saw at most "
        f"{max_in_flight} in flight — probes are running sequentially"
    )
    # The refactor must not change what the probes report, nor the key order:
    # ``/health`` and ``/status`` both splat ``deps`` into their response body,
    # so that order IS the response shape. Checked on the ``deps`` this test
    # already holds rather than in a separate test, which would pay another
    # storage round trip to re-assert a dict literal.
    assert unhealthy == []
    assert list(deps) == ["storage", "redis", "event_bus"]
    assert deps["storage"] == "connected"
    assert deps["redis"] == "connected"
