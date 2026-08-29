"""Authentication boundary for the internal storage service."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from core_storage_api.app import create_app
from core_storage_api.config import settings

pytestmark = pytest.mark.asyncio

_BULK_GET_BODY = {"ids": [], "tenant_id": "storage-auth-test"}


async def test_requests_require_the_storage_shared_secret() -> None:
    app = create_app()
    secret = getattr(
        settings,
        "core_storage_shared_secret",
        SecretStr("test-storage-secret"),
    ).get_secret_value()
    path = "/api/v1/storage/memories/bulk-get"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(path, json=_BULK_GET_BODY)
        wrong = await client.post(
            path,
            json=_BULK_GET_BODY,
            headers={"X-Storage-Secret": "wrong"},
        )
        accepted = await client.post(
            path,
            json=_BULK_GET_BODY,
            headers={"X-Storage-Secret": secret},
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "invalid storage service credentials"}
    assert wrong.status_code == 401
    assert accepted.status_code == 200


async def test_empty_service_secret_fails_closed() -> None:
    with patch.object(settings, "core_storage_shared_secret", SecretStr("")):
        app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/storage/memories/bulk-get",
            json=_BULK_GET_BODY,
            headers={"X-Storage-Secret": "anything"},
        )

    assert response.status_code == 401


async def test_empty_service_secret_fails_readiness_but_not_liveness() -> None:
    with patch.object(settings, "core_storage_shared_secret", SecretStr("")):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            liveness = await client.get("/healthz")
            readiness = await client.get("/readyz")

    assert liveness.status_code == 200
    assert readiness.status_code == 503
    assert readiness.json() == {"detail": "storage service credentials not configured"}


async def test_non_ascii_secret_header_is_refused_not_raised() -> None:
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        request = client.build_request(
            "POST",
            "/api/v1/storage/memories/bulk-get",
            json=_BULK_GET_BODY,
            headers=[(b"X-Storage-Secret", b"\xff")],
        )
        response = await client.send(request)

    assert response.status_code == 401


async def test_duplicate_secret_headers_are_refused() -> None:
    app = create_app()
    secret = settings.core_storage_shared_secret.get_secret_value().encode()
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        request = client.build_request(
            "POST",
            "/api/v1/storage/memories/bulk-get",
            json=_BULK_GET_BODY,
            headers=[
                (b"X-Storage-Secret", secret),
                (b"X-Storage-Secret", secret),
            ],
        )
        response = await client.send(request)

    assert response.status_code == 401


async def test_cors_preflight_reaches_cors_middleware() -> None:
    app = create_app()
    headers = {
        "Origin": "http://localhost:8000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Storage-Secret",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            "/api/v1/storage/memories/bulk-get",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == headers["Origin"]
    assert "x-storage-secret" in response.headers["access-control-allow-headers"].lower()


async def test_cors_wraps_authentication_and_reader_role_responses() -> None:
    with patch.object(settings, "core_storage_role", "reader"):
        app = create_app()
    origin = "http://localhost:8000"
    path = "/api/v1/storage/memories/00000000-0000-0000-0000-000000000000"
    secret = settings.core_storage_shared_secret.get_secret_value()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.patch(path, headers={"Origin": origin})
        read_only = await client.patch(
            path,
            headers={"Origin": origin, "X-Storage-Secret": secret},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["access-control-allow-origin"] == origin
    assert read_only.status_code == 405
    assert read_only.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_public_health_probes_do_not_require_secret(path: str) -> None:
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/healthz/"),
        ("HEAD", "/healthz"),
        ("POST", "/healthz"),
        ("GET", "/api/v1/storage/healthz"),
    ],
)
async def test_only_exact_read_only_health_probes_are_public(method: str, path: str) -> None:
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)

    assert response.status_code == 401
