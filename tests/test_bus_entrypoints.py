"""Optional mounts preserve default imports and storage startup ordering."""

import importlib.util
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]


def test_default_entrypoints_do_not_require_bus():
    code = """
import importlib.abc
import sys
class NoBus(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('caura_bus_'):
            raise AssertionError('default entrypoint imported optional bus')
sys.meta_path.insert(0, NoBus())
from core_api.app import app as api
from core_storage_api.app import app as storage
assert not any('/bus/' in getattr(r, 'path', '') for app in (api, storage) for r in app.routes)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "core-api/src:core-storage-api/src:."},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def module(monkeypatch, name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, value)
    return value


def load(path):
    spec = importlib.util.spec_from_file_location("optional_bus_test", ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


async def test_api_mount_uses_verified_agent_and_human_principals(monkeypatch):
    app = FastAPI()
    app.openapi_schema = {"cached": True}
    mounted = []

    def router(principal, storage, *_args):
        mounted.append((principal, storage))
        return APIRouter()

    module(monkeypatch, "caura_bus_platform")
    module(
        monkeypatch,
        "caura_bus_platform.wake",
        WakeHub=SimpleNamespace,
        publish_wake=None,
    )
    module(
        monkeypatch,
        "caura_bus_platform.routes",
        Operation=SimpleNamespace,
        Principal=SimpleNamespace,
        public_router=router,
    )
    module(
        monkeypatch,
        "caura_bus_platform.collaboration_routes",
        HumanPrincipal=SimpleNamespace,
        human_router=router,
    )
    module(monkeypatch, "core_api.app", app=app)
    module(monkeypatch, "core_api.bus_mcp", register_peer=lambda app: None)
    entry = load("core-api/src/core_api/bus_app.py")
    monkeypatch.setattr(entry.settings, "gateway_shared_secret", "test-gateway")
    auth = SimpleNamespace(
        tenant_id="tenant", agent_id="agent", capabilities={"read"}, org_role="admin"
    )
    request = SimpleNamespace(
        method="GET",
        headers={
            "x-gateway-secret": "test-gateway",
            "x-caura-credential-kind": "agent_key",
        },
    )
    assert (await mounted[0][0](request, auth)).agent_id == "agent"
    with pytest.raises(HTTPException) as denied:
        await mounted[1][0](request, auth)
    assert denied.value.status_code == 403
    request.headers = {"x-gateway-secret": "forged"}
    with pytest.raises(HTTPException) as denied:
        await mounted[0][0](request, auth)
    assert denied.value.status_code == 401
    assert app.openapi_schema is None


@pytest.mark.parametrize("role", ["hybrid", "reader"])
async def test_storage_mount_runs_migrations_inside_existing_lifespan(
    monkeypatch, role
):
    from core_storage_api.config import settings

    monkeypatch.setattr(settings, "core_storage_role", role)
    events = []

    @asynccontextmanager
    async def native_lifespan(_app):
        events.append("native startup")
        yield
        events.append("native shutdown")

    app = FastAPI(lifespan=native_lifespan)
    app.openapi_schema = {"cached": True}

    class Store:
        def __init__(self, engine):
            assert engine == "native engine"

        async def migrate(self):
            events.append("bus migrations")

        async def request_reconciler(self):
            import asyncio

            await asyncio.Event().wait()

    module(monkeypatch, "caura_bus_platform")
    module(
        monkeypatch,
        "caura_bus_platform.wake",
        WakeHub=SimpleNamespace,
        publish_wake=None,
    )
    module(
        monkeypatch,
        "caura_bus_platform.routes",
        storage_router=lambda _store: APIRouter(),
    )
    module(monkeypatch, "caura_bus_platform.store", Store=Store)
    module(monkeypatch, "core_storage_api.app", app=app)
    module(
        monkeypatch,
        "core_storage_api.database.init",
        get_engine=lambda: "native engine",
    )
    entry = load("core-storage-api/src/core_storage_api/bus_app.py")
    async with entry.app.router.lifespan_context(app):
        assert events == (
            ["native startup"]
            if role == "reader"
            else ["native startup", "bus migrations"]
        )
    assert events == (
        ["native startup", "native shutdown"]
        if role == "reader"
        else ["native startup", "bus migrations", "native shutdown"]
    )
    assert (entry.store is None) == (role == "reader")
    assert app.openapi_schema is None
