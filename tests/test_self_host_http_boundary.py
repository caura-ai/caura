"""HTTP transport and credential boundaries for standalone installs."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from core_api import auth, mcp_server
from core_api.app import app
from core_api.config import settings
from core_api.routes.health import _gateway_verified

pytestmark = pytest.mark.unit
_SHARED_KEY_FIELD = "memclaw_api_key"  # legacy-name-ok: resolved dual-read setting


@pytest.fixture(autouse=True)
def standalone_tenant(monkeypatch):
    from core_api import _standalone_state

    monkeypatch.setattr(_standalone_state, "standalone_tenant_id", "standalone-test")


@pytest.mark.parametrize("path", ["/api/v1/version", "/mcp/"])
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"host": "untrusted.example"}, 421),
        ({"host": "localhost:8000", "origin": "https://untrusted.example"}, 403),
    ],
)
async def test_standalone_rejects_foreign_authority(monkeypatch, path, headers, status):
    monkeypatch.setattr(settings, "is_standalone", True)
    if path == "/mcp/":
        from mcp.server.transport_security import TransportSecurityMiddleware

        response = await TransportSecurityMiddleware(
            mcp_server.mcp.session_manager.security_settings
        ).validate_request(
            Request(
                {
                    "type": "http",
                    "headers": [
                        (key.encode(), value.encode()) for key, value in headers.items()
                    ],
                }
            )
        )
        assert response is not None
        assert response.status_code == status
        return
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == status


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"]
)
async def test_standalone_accepts_loopback(monkeypatch, host):
    monkeypatch.setattr(settings, "is_standalone", True)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/v1/version", headers={"host": host})
    assert response.status_code == 200


def test_mcp_sdk_checks_standalone_authority():
    # The mount must retain the SDK check as well as the REST boundary.
    manager = mcp_server.mcp.session_manager
    assert manager.security_settings.enable_dns_rebinding_protection


@pytest.mark.parametrize("mode", ["admin", "shared", "gateway"])
async def test_rest_non_ascii_credentials_are_401(monkeypatch, mode):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, "admin_api_key", "admin")
    monkeypatch.setattr(
        settings, _SHARED_KEY_FIELD, "shared" if mode == "shared" else None
    )
    monkeypatch.setattr(settings, "gateway_shared_secret", "gateway")
    headers = [(b"x-tenant-id", b"tenant")]
    key = "\xe9" if mode != "gateway" else None
    if mode == "gateway":
        headers.append((b"x-gateway-secret", b"\xff"))
    request = Request({"type": "http", "headers": headers})
    with pytest.raises(HTTPException) as error:
        await auth.get_auth_context(request, key)
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "header", [b"x-api-key", b"authorization", b"x-gateway-secret"]
)
async def test_mcp_non_ascii_credentials_are_401(monkeypatch, header):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, "admin_api_key", "admin")
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    monkeypatch.setattr(settings, "gateway_shared_secret", "gateway")
    downstream = AsyncMock()
    sent = []
    value = b"Bearer \xff" if header == b"authorization" else b"\xff"
    await mcp_server.MCPAuthMiddleware(downstream)(
        {"type": "http", "headers": [(b"x-tenant-id", b"tenant"), (header, value)]},
        AsyncMock(return_value={"type": "http.request", "body": b""}),
        AsyncMock(side_effect=sent.append),
    )
    downstream.assert_not_awaited()
    assert (
        next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]
        == 401
    )


def test_whoami_non_ascii_perimeter_is_unverified(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "gateway")
    assert not _gateway_verified(
        Request({"type": "http", "headers": [(b"x-gateway-secret", b"\xff")]})
    )


@pytest.mark.parametrize("name", ["bus_principal", "human_principal"])
async def test_bus_non_ascii_perimeter_is_401(monkeypatch, name):
    import ast
    from pathlib import Path

    # The optional bus application needs enterprise-only packages. Execute its
    # real principal function without importing that unrelated application.
    source = Path(auth.__file__).with_name("bus_app.py").read_text()
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    for argument in function.args.args:
        argument.annotation = None
    function.returns = None
    namespace = {
        "settings": settings,
        "HTTPException": HTTPException,
        "credential_matches": auth.credential_matches,
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "bus_app.py", "exec"),
        namespace,
    )
    monkeypatch.setattr(settings, "gateway_shared_secret", "gateway")
    request = Request({"type": "http", "headers": [(b"x-gateway-secret", b"\xff")]})
    with pytest.raises(HTTPException) as error:
        await namespace[name](request, auth.AuthContext(tenant_id="tenant"))
    assert error.value.status_code == 401


@pytest.fixture
def browser_boundary_app(monkeypatch, request):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from core_api.http_security import StandaloneHTTPBoundary

    monkeypatch.setattr(settings, "is_standalone", True)
    monkeypatch.setattr(
        settings,
        "cors_origins",
        getattr(
            request, "param", " https://dashboard.example, , https://second.example "
        ),
    )
    browser_app = FastAPI()

    @browser_app.get("/probe")
    async def probe():
        return {"ok": True}

    browser_app.add_middleware(StandaloneHTTPBoundary)
    browser_app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            value.strip() for value in settings.cors_origins.split(",") if value.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return browser_app


@pytest.mark.parametrize(
    "origin", ["https://dashboard.example", "https://second.example"]
)
async def test_cors_origin_passes_preflight_and_actual_request(
    browser_boundary_app, origin
):
    async with AsyncClient(
        transport=ASGITransport(app=browser_boundary_app),
        base_url="http://localhost:8000",
    ) as client:
        preflight = await client.options(
            "/probe", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
        )
        assert preflight.status_code == 200
        actual = await client.get("/probe", headers={"Origin": origin})
    assert actual.status_code == 200
    assert actual.json() == {"ok": True}


@pytest.mark.parametrize(
    "origin", ["https://dashboard.example", "https://second.example"]
)
async def test_mcp_policy_accepts_configured_cors_origin(browser_boundary_app, origin):
    from mcp.server.transport_security import TransportSecurityMiddleware

    from core_api.http_security import transport_security_settings

    policy = transport_security_settings()
    response = await TransportSecurityMiddleware(policy).validate_request(
        Request(
            {
                "type": "http",
                "headers": [(b"host", b"localhost:8000"), (b"origin", origin.encode())],
            }
        )
    )
    assert response is None
    assert "*" not in policy.allowed_origins
    assert "" not in policy.allowed_origins


async def test_cors_origin_does_not_allow_its_host(browser_boundary_app):
    async with AsyncClient(
        transport=ASGITransport(app=browser_boundary_app),
        base_url="http://dashboard.example",
    ) as client:
        response = await client.get(
            "/probe", headers={"Origin": "https://dashboard.example"}
        )
    assert response.status_code == 421


@pytest.mark.parametrize(
    "browser_boundary_app", ["*, https://dashboard.example"], indirect=True
)
async def test_cors_wildcard_keeps_unlisted_origins_rejected(browser_boundary_app):
    from core_api.http_security import transport_security_settings

    assert "*" not in transport_security_settings().allowed_origins
    async with AsyncClient(
        transport=ASGITransport(app=browser_boundary_app),
        base_url="http://localhost:8000",
    ) as client:
        preflight = await client.options(
            "/probe",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        actual = await client.get(
            "/probe", headers={"Origin": "https://untrusted.example"}
        )
    assert actual.status_code == 403
