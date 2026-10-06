"""Single writes are charged only once the write has succeeded.

REST ``POST /memories/bulk`` moved its charge after the write when one
never-written batch was billed once per retry. The single-write paths (MCP
``caura_write(content=...)`` and REST ``POST /memories``) still charged first,
so a write that raised — rejected, a duplicate, a storage failure — cost a
write unit per attempt while writing nothing.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import get_test_auth, uid


class _OutStub:
    def model_dump(self, mode: str = "python"):
        return {"id": "m-1", "status": "created"}


@pytest.fixture
def mcp_charges(monkeypatch):
    charged: list[tuple] = []

    async def _spy(tenant_id, operation, *a, **kw):
        charged.append((tenant_id, operation))

    monkeypatch.setattr(mcp_server, "check_and_increment", _spy)
    return charged


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_single_write_that_raised_is_not_charged(mcp_env, mcp_charges):
    mcp_env["service"]("create_memory").side_effect = HTTPException(
        status_code=504, detail="storage timed out"
    )

    for _ in range(3):
        out = await mcp_server.caura_write(content="a single fact")
        assert "error" in parse_envelope(out)

    assert mcp_charges == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_single_write_is_charged_after_it_succeeds(mcp_env, mcp_charges):
    order: list[str] = []

    async def _write(*a, **kw):
        order.append("write")
        return _OutStub()

    mcp_env["service"]("create_memory").side_effect = _write

    out = await mcp_server.caura_write(content="a single fact")

    assert "error" not in parse_envelope(out)
    assert mcp_charges == [(mcp_env["tenant"], "write")]
    assert order == ["write"]


def _as_tenant(monkeypatch, tenant_id):
    """A TENANT-scoped credential: the admin key skips metering entirely, so a
    "was not charged" assertion under it would pass whatever the code did."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    monkeypatch.setitem(
        app.dependency_overrides,
        get_auth_context,
        lambda: AuthContext(tenant_id=tenant_id, org_role="admin"),
    )


@pytest.fixture
def rest_charges(monkeypatch):
    from core_api.routes import memories as routes_mem

    calls: list[tuple] = []

    async def recorder(tenant_id, operation, count=1):
        calls.append((tenant_id, operation))
        return None

    monkeypatch.setattr(routes_mem, "check_and_increment", recorder)
    return calls


@pytest.mark.asyncio
async def test_rest_single_write_that_raised_is_not_charged(
    client, monkeypatch, rest_charges
):
    from core_api.routes import memories as routes_mem

    async def _fail(body):
        raise HTTPException(status_code=504, detail="storage timed out")

    monkeypatch.setattr(routes_mem, "create_memory", _fail)
    _as_tenant(monkeypatch, "default")
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"meter-single-fail-{uid()}",
        "memory_type": "fact",
        "content": f"meter single fail {uid()}",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)

    assert resp.status_code == 504, resp.text
    assert rest_charges == []


@pytest.mark.asyncio
async def test_rest_single_write_is_still_charged_on_success(
    client, monkeypatch, rest_charges
):
    _as_tenant(monkeypatch, "default")
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"meter-single-ok-{uid()}",
        "memory_type": "fact",
        "content": f"meter single ok {uid()}",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)

    assert resp.status_code == 201, resp.text
    assert rest_charges == [(tenant_id, "write")]
