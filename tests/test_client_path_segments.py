"""Storage client IDs stay in one segment while tenant routing stays explicit."""

from urllib.parse import quote

import pytest

from tests.test_storage_client_routing import _fresh_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "identifier", ["a/b?tenant_id=other#tail", "../sibling", "has space%"]
)
@pytest.mark.parametrize("operation", ["get", "resolve", "fleet"])
async def test_storage_client_encodes_identifiers(identifier, operation):
    client, writer, reader = await _fresh_client(
        "http://writer:8002", "http://reader:8002"
    )
    try:
        if operation == "get":
            await client.get_memory_conflict(identifier, "tenant-one")
        elif operation == "resolve":
            await client.resolve_memory_conflict(
                identifier, {"tenant_id": "tenant-one"}
            )
        else:
            await client.delete_fleet("tenant-one", identifier)
        (request,) = writer.requests + reader.requests
        prefix = (
            "/api/v1/storage/fleet/"
            if operation == "fleet"
            else "/api/v1/storage/memories/memory-conflicts/"
        )
        path = (
            prefix
            + quote(identifier, safe="")
            + ("/resolve" if operation == "resolve" else "")
        )
        assert request.url.raw_path.decode().split("?")[0] == path
        assert not request.url.fragment
        if operation != "resolve":
            assert dict(request.url.params) == {"tenant_id": "tenant-one"}
    finally:
        await client._http.aclose()
        await client._read_http.aclose()
