"""Optional core mounts expose fixed errors, never internal exception payloads."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException


@pytest.fixture
def entry(monkeypatch):
    class AuthContext:
        pass

    modules = {
        "caura_bus_platform": {},
        "caura_bus_platform.wake": {"WakeHub": SimpleNamespace},
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
        "core_api.auth": {"AuthContext": AuthContext, "get_auth_context": lambda: None},
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
