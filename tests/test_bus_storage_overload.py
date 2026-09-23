"""SQL pool exhaustion at the optional storage mount is a retryable response."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import TimeoutError as PoolTimeout

pytestmark = pytest.mark.unit


@pytest.fixture
def storage_app(monkeypatch):
    app = FastAPI()
    for name, values in {
        "core_storage_api": {},
        "core_storage_api.app": {"app": app},
        "core_storage_api.config": {
            "settings": SimpleNamespace(core_storage_role="reader"),
            "db_connect_args": lambda url: {},
        },
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    path = (
        Path(__file__).resolve().parents[1]
        / "core-storage-api/src/core_storage_api/bus_app.py"
    )
    spec = importlib.util.spec_from_file_location("storage_overload_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return app


async def test_pool_timeout_is_private_retryable_and_does_not_poison_app(storage_app):
    @storage_app.post("/api/v1/storage/bus/execute")
    async def overloaded():
        raise PoolTimeout("private connection details")

    @storage_app.get("/health")
    async def health():
        return {"status": "ok"}

    @storage_app.post("/unrelated")
    async def unrelated():
        raise PoolTimeout("private connection details")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(storage_app, raise_app_exceptions=False),
        base_url="http://storage",
    ) as client:
        for _ in range(2):
            result = await client.post("/api/v1/storage/bus/execute")
            assert result.status_code == 503 and result.headers["retry-after"] == "1"
            assert result.json()["error"]["code"] == "COLLABORATION_UNAVAILABLE"
            assert "private" not in result.text
            assert (await client.get("/health")).status_code == 200
        assert (await client.post("/unrelated")).status_code == 500
