"""Optional core mounts expose fixed errors, never internal exception payloads."""

import importlib.util
import sys
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException

pytestmark = pytest.mark.unit
SUPPRESSION_PATH = Path(__file__).resolve().parents[1] / "core-api/src/core_api/suppression.py"


@pytest.fixture
def entry(monkeypatch):
    from caura_bus_platform.liveness import SuppressionCache

    class AuthContext:
        pass

    modules = {
        "caura_bus_platform": {},
        "caura_bus_platform.wake": {"WakeHub": SimpleNamespace},
        "caura_bus_platform.liveness": {"SuppressionCache": SuppressionCache},
        "core_api.suppression": {
            "use_suppression_lookup": lambda lookup: nullcontext()
        },
        "caura_bus_platform.timing": {
            "TimingMiddleware": lambda app, **kwargs: app,
            "span": lambda name: nullcontext(),
        },
        "caura_bus_platform.runtime": {
            "AdmissionMiddleware": SimpleNamespace,
            "send_deadline": ContextVar("test_send_deadline", default=None),
            "Runtime": lambda *_args: SimpleNamespace(install=lambda app: None),
            "shutdown_signals": lambda *_args: None,
            "stop_task": lambda *_args: None,
        },
        "caura_bus_platform.settings": {
            "settings": SimpleNamespace(request_timeout_seconds=25)
        },
        "core_api.bus_storage": {
            "get_storage_client": lambda: None,
            "get_presence_storage_client": lambda: None,
            "close_storage_client": lambda: None,
        },
        "core_api.middleware": {},
        "core_api.middleware.request_timeout": {
            "RequestTimeoutMiddleware": SimpleNamespace
        },
        "caura_bus_platform.routes": {
            "Operation": SimpleNamespace,
            "Principal": SimpleNamespace,
            "public_router": lambda *_args: APIRouter(),
        },
        "caura_bus_platform.collaboration_routes": {
            "HumanPrincipal": SimpleNamespace,
            "human_router": lambda *_args: APIRouter(),
        },
        "core_api": {},
        "core_api.app": {"app": FastAPI()},
        "core_api.auth": {
            "AuthContext": AuthContext,
            "get_auth_context": lambda: None,
            "api_key_header": None,
        },
        "core_api.clients": {},
        "core_api.clients.storage_client": {"get_storage_client": lambda: None},
        "core_api.config": {"settings": SimpleNamespace(gateway_shared_secret="test")},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / "core-api/src/core_api/bus_app.py"
    spec = importlib.util.spec_from_file_location("storage_error_test", path)
    module = importlib.util.module_from_spec(spec)
    fake_mcp = ModuleType("core_api.bus_mcp")
    fake_mcp.register_peer = lambda app: None
    monkeypatch.setitem(sys.modules, "core_api.bus_mcp", fake_mcp)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status", [403, 404, 409, 422, 500, 502, 503])
@pytest.mark.parametrize(
    "payload", [{"detail": "private path and credential"}, "not an object"]
)
async def test_upstream_error_payload_never_reaches_client(
    entry, monkeypatch, status, payload
):
    async def fail(*_args, **_kwargs):
        response = httpx.Response(
            status, json=payload, request=httpx.Request("POST", "http://storage")
        )
        raise httpx.HTTPStatusError(
            "private diagnostic", request=response.request, response=response
        )

    monkeypatch.setattr(
        entry, "get_storage_client", lambda: SimpleNamespace(_post=fail)
    )
    operation = SimpleNamespace(operation="wait", model_dump=lambda: {})
    with pytest.raises(HTTPException) as caught:
        await entry.storage_call(operation)
    assert caught.value.status_code == (status if status < 500 else 503)
    assert "private" not in str(caught.value.detail)
    assert "not an object" not in str(caught.value.detail)


async def test_pause_context_is_rebuilt_without_arbitrary_fields(entry, monkeypatch):
    async def fail(*_args, **_kwargs):
        response = httpx.Response(
            409,
            json={
                "detail": {
                    "state": "paused",
                    "lease_token": "private",
                    "internal": "private",
                }
            },
            request=httpx.Request("POST", "http://storage"),
        )
        raise httpx.HTTPStatusError(
            "private", request=response.request, response=response
        )

    monkeypatch.setattr(
        entry, "get_storage_client", lambda: SimpleNamespace(_post=fail)
    )
    with pytest.raises(HTTPException) as caught:
        await entry.storage_call(
            SimpleNamespace(operation="wait", model_dump=lambda: {})
        )
    assert caught.value.detail == {
        "state": "paused",
        "detail": "Delivery paused; call wait for current context",
    }


@pytest.mark.parametrize(
    "code",
    [
        "REQUEST_NOT_DECIDABLE",
        "TARGET_UNAVAILABLE",
        "TARGET_ALREADY_ASSIGNED",
        "STOP_NOT_CONFIRMED",
    ],
)
async def test_human_decision_conflict_rebuilds_fixed_message(entry, monkeypatch, code):
    async def fail(*_args, **_kwargs):
        response = httpx.Response(
            409,
            json={
                "detail": {
                    "code": code,
                    "message": "private diagnostic",
                    "lease_token": "private",
                }
            },
            request=httpx.Request("POST", "http://storage"),
        )
        raise httpx.HTTPStatusError(
            "private", request=response.request, response=response
        )

    monkeypatch.setattr(
        entry, "get_storage_client", lambda: SimpleNamespace(_post=fail)
    )
    with pytest.raises(HTTPException) as caught:
        await entry.storage_call(
            SimpleNamespace(operation="human_decide", model_dump=lambda: {})
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": code,
        "message": entry.DECISION_CONFLICTS[code],
    }
    assert "private" not in str(caught.value.detail)


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        ("human_decide", "UNKNOWN_PRIVATE_CODE"),
        ("human_decide", ["TARGET_UNAVAILABLE"]),
        ("wait", "TARGET_UNAVAILABLE"),
    ],
)
async def test_conflict_code_allowlist_is_limited_to_human_decisions(
    entry, monkeypatch, operation, code
):
    async def fail(*_args, **_kwargs):
        response = httpx.Response(
            409,
            json={"detail": {"code": code, "message": "private diagnostic"}},
            request=httpx.Request("POST", "http://storage"),
        )
        raise httpx.HTTPStatusError(
            "private", request=response.request, response=response
        )

    monkeypatch.setattr(
        entry, "get_storage_client", lambda: SimpleNamespace(_post=fail)
    )
    with pytest.raises(HTTPException) as caught:
        await entry.storage_call(
            SimpleNamespace(operation=operation, model_dump=lambda: {})
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "Caura operation conflicts with the current state"


async def test_presence_routes_to_its_reserved_client(entry, monkeypatch):
    async def presence_post(path, payload, **kwargs):
        assert path == "/bus/execute" and payload == {"operation": "presence"}
        return {"ttl_seconds": 45}

    monkeypatch.setattr(
        entry,
        "get_storage_client",
        lambda: pytest.fail("message pool used for presence"),
    )
    monkeypatch.setattr(
        entry,
        "get_presence_storage_client",
        lambda: SimpleNamespace(_post=presence_post),
    )
    result = await entry.storage_call(
        SimpleNamespace(
            operation="presence", model_dump=lambda: {"operation": "presence"}
        )
    )
    assert result == {"ttl_seconds": 45}


async def test_cold_suppression_and_presence_use_reserved_pool_under_real_tcp_saturation(
    entry, monkeypatch
):
    import asyncio

    from starlette.requests import Request

    # Load the real suppression boundary with the fixture's storage getter.
    spec = importlib.util.spec_from_file_location("scoped_suppression_test", SUPPRESSION_PATH)
    suppression = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suppression)
    monkeypatch.setattr(
        entry, "use_suppression_lookup", suppression.use_suppression_lookup
    )
    blocked, release = asyncio.Event(), asyncio.Event()
    paths = []

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ")[1]
            paths.append(path)
            if path == b"/held":
                blocked.set()
                await release.wait()
            payload = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with (
        server,
        httpx.AsyncClient(
            limits=httpx.Limits(max_connections=1), trust_env=False
        ) as message,
        httpx.AsyncClient(
            limits=httpx.Limits(max_connections=1), trust_env=False
        ) as reserved,
    ):
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

        async def lookup(tenant):
            response = await reserved.get(url + "/suppression")
            response.raise_for_status()
            return False

        async def presence(*args, **kwargs):
            response = await reserved.get(url + "/presence")
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(
            entry,
            "get_presence_storage_client",
            lambda: SimpleNamespace(is_tenant_suppressed=lookup, _post=presence),
        )

        async def auth(request, key):
            assert not await suppression.is_tenant_suppressed("tenant")
            return "authenticated"

        monkeypatch.setattr(entry, "get_auth_context", auth)
        held = asyncio.create_task(message.get(url + "/held"))
        await blocked.wait()
        try:
            async with asyncio.timeout(1):
                request = Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/api/v1/bus/presence",
                        "headers": [],
                    }
                )
                assert (
                    await entry.measured_auth_context(request, "private-test-key")
                    == "authenticated"
                )
                assert await entry.storage_call(
                    SimpleNamespace(operation="presence", model_dump=lambda: {})
                ) == {"ok": True}
            assert not held.done() and paths == [
                b"/held",
                b"/suppression",
                b"/presence",
            ]
        finally:
            release.set()
            await held
            await entry.suppression_cache.close()
