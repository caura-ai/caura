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
