"""Evidence contract v1 (EC-07/08/09): the evidence fields on the write and read paths.

A writer may send ``observed_at``, ``trace_id`` and ``tool_call_id``; they reach
the row and come back on every full read. A write without a ``trace_id`` is
stamped with its MCP session, its ``X-Caura-Session-Id``, or an id for the one
request. The verification fields are server-owned: a create, a bulk item or a
PATCH that names one is refused with a message that says so.

See ``docs/plans/evidence-contract-v1.md``.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from core_api import session_context
from core_api.schemas import (
    BulkMemoryCreate,
    BulkMemoryItem,
    MemoryCreate,
    MemoryUpdate,
)
from core_api.services.evidence import evidence_payload
from tests.conftest import get_admin_headers, uid

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)
_OBSERVED = "2026-11-01T09:30:00+00:00"


def _tenant() -> str:
    # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
    return f"test-tenant-evidence-{uuid.uuid4().hex[:8]}"


# ── EC-09: server-owned verification fields ───────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("field", ["verification_status", "verified_by", "verified_at"])
def test_create_refuses_a_verification_field_and_says_the_server_sets_it(
    field: str,
) -> None:
    with pytest.raises(ValidationError) as e:
        MemoryCreate(
            tenant_id="t1", agent_id="a1", content="x", **{field: "human_verified"}
        )
    msg = str(e.value)
    assert field in msg and "set by the server" in msg
    # It names what a writer CAN do, not supersession, which is not the topic.
    assert "source_uri or tool_call_id" in msg
    assert "contradiction detection" not in msg


@pytest.mark.unit
def test_update_refuses_a_verification_field() -> None:
    with pytest.raises(ValidationError) as e:
        MemoryUpdate(verification_status="human_verified")
    assert "set by the server" in str(e.value)


@pytest.mark.unit
def test_two_server_owned_fields_get_both_explanations() -> None:
    with pytest.raises(ValidationError) as e:
        MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="x",
            supersedes_id=str(uuid.uuid4()),
            verified_by="me",
        )
    msg = str(e.value)
    assert "supersedes_id, verified_by are set by the server" in msg
    assert "contradiction detection" in msg and "source_uri or tool_call_id" in msg


@pytest.mark.unit
def test_the_writer_evidence_fields_are_accepted_and_bounded() -> None:
    m = MemoryCreate(
        tenant_id="t1",
        agent_id="a1",
        content="x",
        observed_at=_OBSERVED,
        trace_id="trace-1",
        tool_call_id="toolu_01",
    )
    assert evidence_payload(m) == {
        "observed_at": _OBSERVED,
        "trace_id": "trace-1",
        "tool_call_id": "toolu_01",
    }
    with pytest.raises(ValidationError):
        MemoryCreate(tenant_id="t1", agent_id="a1", content="x", trace_id="x" * 201)


# ── trace_id stamping ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_server_trace_id_is_the_session_then_the_request() -> None:
    session_context.set_session(None, None)
    session_context.set_request_id(None)
    assert session_context.server_trace_id() is None
    session_context.set_request_id("req-1")
    assert session_context.server_trace_id() == "req-1"
    session_context.set_session("mcp-9", "mcp")
    assert session_context.server_trace_id() == "mcp-9"


@pytest.mark.unit
async def test_the_rest_middleware_issues_a_fresh_request_id_per_request() -> None:
    seen: list[str | None] = []

    async def app(scope, receive, send):
        seen.append(session_context.current_request_id())

    mw = session_context.SessionHeaderMiddleware(app)
    for _ in range(2):
        await mw({"type": "http", "headers": []}, None, None)
    assert all(s and s.startswith("req-") for s in seen) and seen[0] != seen[1]


# ── REST round trip ───────────────────────────────────────────────────────


@pytest.mark.integration
async def test_rest_write_persists_the_evidence_and_both_reads_return_it(
    client, tenant_id
) -> None:
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-agent",
            "memory_type": "fact",
            "content": f"evidence round trip {uid()}." + _PADDING,
            "source_uri": "https://example.com/doc",
            "observed_at": _OBSERVED,
            "trace_id": "client-trace-7",
            "tool_call_id": "toolu_abc",
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    out = w.json()
    assert out["trace_id"] == "client-trace-7"
    assert out["tool_call_id"] == "toolu_abc"
    assert out["observed_at"].startswith("2026-11-01T09:30:00")
    assert out["verification_status"] == "unverified"
    assert out["verified_by"] is None and out["verified_at"] is None

    r = await client.get(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}", headers=headers
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["observed_at"] == _OBSERVED
    assert (
        detail["trace_id"],
        detail["tool_call_id"],
        detail["verification_status"],
    ) == (
        "client-trace-7",
        "toolu_abc",
        "unverified",
    )


@pytest.mark.integration
async def test_rest_write_without_a_trace_id_is_stamped_with_the_session_header(
    client, tenant_id
) -> None:
    headers = {**get_admin_headers(), "X-Caura-Session-Id": "sess-ec08"}
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-agent",
            "content": f"session stamped {uid()}." + _PADDING,
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    assert w.json()["trace_id"] == "sess-ec08"


@pytest.mark.integration
async def test_rest_write_with_no_session_is_stamped_with_a_request_id(
    client, tenant_id
) -> None:
    headers = get_admin_headers()
    ids = []
    for _ in range(2):
        w = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": "evidence-agent",
                "content": f"request stamped {uid()}." + _PADDING,
            },
            headers=headers,
        )
        assert w.status_code in (200, 201), w.text
        ids.append(w.json()["trace_id"])
    assert all(i and i.startswith("req-") for i in ids)
    assert ids[0] != ids[1], "two requests were stamped with one trace_id"


@pytest.mark.integration
async def test_rest_create_with_a_verification_field_is_a_422_naming_it(
    client, tenant_id
) -> None:
    r = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-agent",
            "content": f"self verified {uid()}." + _PADDING,
            "verification_status": "human_verified",
        },
        headers=get_admin_headers(),
    )
    assert r.status_code == 422, r.text
    assert "set by the server" in r.text


@pytest.mark.integration
async def test_rest_patch_with_a_verification_field_is_a_422(client, tenant_id) -> None:
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-agent",
            "content": f"patch target {uid()}." + _PADDING,
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    r = await client.patch(
        f"/api/v1/memories/{w.json()['id']}?tenant_id={tenant_id}",
        json={"verification_status": "human_verified"},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "set by the server" in r.text
    detail = (
        await client.get(
            f"/api/v1/memories/{w.json()['id']}?tenant_id={tenant_id}", headers=headers
        )
    ).json()
    assert detail["verification_status"] == "unverified"


# ── Bulk ──────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_bulk_items_keep_their_own_trace_id_or_share_the_batch_one(
    client, tenant_id
) -> None:
    headers = {**get_admin_headers(), "X-Bulk-Attempt-Id": str(uuid.uuid4())}
    r = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-agent",
            "items": [
                {
                    "content": f"bulk own trace {uid()}." + _PADDING,
                    "trace_id": "item-trace",
                    "tool_call_id": "call_1",
                    "observed_at": _OBSERVED,
                },
                {"content": f"bulk batch trace a {uid()}." + _PADDING},
                {"content": f"bulk batch trace b {uid()}." + _PADDING},
                {
                    "content": f"bulk self verified {uid()}." + _PADDING,
                    "verified_by": "me",
                },
            ],
        },
        headers=headers,
    )
    assert r.status_code in (200, 207), r.text
    results = r.json()["results"]
    assert [x["status"] for x in results] == ["created", "created", "created", "error"]
    assert "set by the server" in results[3]["error"]

    rows = []
    for res in results[:3]:
        g = await client.get(
            f"/api/v1/memories/{res['id']}?tenant_id={tenant_id}", headers=headers
        )
        assert g.status_code == 200, g.text
        rows.append(g.json())
    assert (rows[0]["trace_id"], rows[0]["tool_call_id"], rows[0]["observed_at"]) == (
        "item-trace",
        "call_1",
        _OBSERVED,
    )
    assert rows[1]["trace_id"] and rows[1]["trace_id"].startswith("req-")
    assert rows[1]["trace_id"] == rows[2]["trace_id"], "one batch is one request"


@pytest.mark.unit
def test_a_bulk_item_accepts_the_writer_evidence_fields() -> None:
    body = BulkMemoryCreate(
        tenant_id="t1",
        agent_id="a1",
        items=[
            BulkMemoryItem(
                content="x", trace_id="t", tool_call_id="c", observed_at=_OBSERVED
            )
        ],
    )
    item = body.items[0]
    assert not item.model_extra
    assert evidence_payload(item)["observed_at"] == _OBSERVED


# ── MCP ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mcp_identity(monkeypatch):
    from core_api import mcp_server

    tenant = _tenant()
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_check_write_scope", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: "test-agent")
    return tenant


@pytest.mark.integration
async def test_mcp_write_without_a_trace_id_is_stamped_with_the_mcp_session(
    db, mcp_identity
) -> None:
    from sqlalchemy import text

    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope

    # What ``run_with_mcp_session`` sets for a request that sent no Mcp-Session-Id.
    session_context.set_session("mcp-ec08session", "mcp")
    out = parse_envelope(
        await mcp_server.caura_write(
            content="The Atlantic separates Europe from the Americas." + _PADDING,
            agent_id="test-agent",
            fleet_id="test-fleet",
            tool_call_id="toolu_mcp",
            observed_at=_OBSERVED,
        )
    )
    assert "error" not in out, out
    assert out["trace_id"] == "mcp-ec08session"
    assert out["tool_call_id"] == "toolu_mcp"
    assert out["verification_status"] == "unverified"

    row = (
        await db.execute(
            text(
                "SELECT trace_id, tool_call_id, observed_at, verification_status FROM memories WHERE id = :id"
            ),
            {"id": out["id"]},
        )
    ).one()
    assert (row.trace_id, row.tool_call_id, row.verification_status) == (
        "mcp-ec08session",
        "toolu_mcp",
        "unverified",
    )
    assert row.observed_at.isoformat() == _OBSERVED


@pytest.mark.integration
async def test_mcp_write_keeps_the_callers_trace_id(mcp_identity) -> None:
    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope

    session_context.set_session("mcp-other", "mcp")
    out = parse_envelope(
        await mcp_server.caura_write(
            content="Mount Everest is the highest mountain above sea level." + _PADDING,
            agent_id="test-agent",
            fleet_id="test-fleet",
            trace_id="otel-4bf92f3577b34da6",
        )
    )
    assert out["trace_id"] == "otel-4bf92f3577b34da6"
