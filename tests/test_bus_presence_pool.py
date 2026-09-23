"""A full message HTTP pool cannot starve a heartbeat request."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "core-api/src/core_api/bus_storage.py"
)


async def test_presence_http_pool_remains_available_and_both_clients_close(monkeypatch):
    class BaseClient:
        def __init__(self, **kwargs):
            self._http = self._make_pool()

        async def close(self):
            await self._http.aclose()

    client_module = ModuleType("core_api.clients.storage_client")
    client_module.CoreStorageClient = BaseClient
    client_module._client = None
    package = ModuleType("core_api.clients")
    package.storage_client = client_module
    monkeypatch.setitem(sys.modules, "core_api", ModuleType("core_api"))
    monkeypatch.setitem(sys.modules, "core_api.clients", package)
    monkeypatch.setitem(sys.modules, "core_api.clients.storage_client", client_module)
    settings_module = ModuleType("caura_bus_platform.settings")
    settings_module.settings = SimpleNamespace(
        http_pool_size=1,
        http_keepalive=1,
        http_pool_timeout=0.05,
        presence_http_pool_size=1,
    )
    monkeypatch.setitem(sys.modules, "caura_bus_platform.settings", settings_module)
    spec = importlib.util.spec_from_file_location("presence_pool_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    message, presence = (
        module.get_storage_client(),
        module.get_presence_storage_client(),
    )
    assert message is not presence and client_module._client is message
    assert module.get_presence_storage_client() is presence
    blocked, release = asyncio.Event(), asyncio.Event()

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if request.startswith(b"GET /message "):
                blocked.set()
                await release.wait()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        async with server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            task = asyncio.create_task(message._http.get(url + "/message"))
            try:
                await asyncio.wait_for(blocked.wait(), 1)
                result = await asyncio.wait_for(
                    presence._http.get(url + "/presence"), 1
                )
                assert result.status_code == 200 and not task.done()
            finally:
                release.set()
                await task
    finally:
        await module.close_storage_client()
    assert message._http.is_closed and presence._http.is_closed
    assert module._client is module._presence_client is client_module._client is None
