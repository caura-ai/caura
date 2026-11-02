"""The tenant's PII policy covers documents and keystones, not only memories.

``POST /documents`` and MCP ``caura_doc`` (op=write) store a caller's ``data``;
``POST /keystones`` and MCP ``caura_keystones_set`` store a rule's title and
text, which every agent in scope receives at session start. Each applies
``governance.pii`` before anything is stored, as a memory write does: ``drop``
refuses the write, ``mask`` stores the redacted text, and ``flag`` stores it as
sent. Every action leaves one ``pii_*`` audit row whose ``write_mode`` names the
store, so a document's own row is told apart from the one its minted memory
writes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import core_api.services.organization_settings as ts_svc
from core_api import mcp_server
from core_api.services.organization_settings import invalidate_cache, update_settings
from core_storage_api.services.postgres_service import get_session
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_EMAIL = "john.doe@example.com"
_CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """The settings cache is per-process: clear it so the policy a test seeds is
    the one the route reads."""
    ts_svc._settings_cache.clear()
    yield
    ts_svc._settings_cache.clear()


async def _tenant_with_pii_action(action: str) -> tuple[str, dict]:
    # ``test-tenant-`` rows are swept by the conftest teardown.
    tenant_id, headers = get_test_auth(f"test-tenant-pii-{_uid()}")
    await update_settings(
        tenant_id, {"governance": {"pii": {"enabled": True, "action": action}}}
    )
    invalidate_cache(tenant_id)
    return tenant_id, headers


async def _pii_audit(tenant_id: str, write_mode: str) -> list[dict]:
    """The ``pii_*`` audit rows one store's writes left, oldest first."""
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT action, detail FROM audit_log "
                    "WHERE tenant_id=:t AND action LIKE 'pii_%' ORDER BY seq"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [
        {"action": action, "detail": detail}
        for action, detail in rows
        if detail.get("write_mode") == write_mode
    ]


def _doc_data() -> dict:
    return {
        "summary": "Escalation contacts for the billing runbook",
        "contact": {"team": "Billing on-call", "email": _EMAIL},
        "notes": [f"Refund test card {_CARD}", "Page the on-call first"],
    }


async def _write_doc(client, headers, tenant_id, data):
    return await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "runbooks",
            "doc_id": "billing",
            "data": data,
        },
        headers=headers,
    )


async def _read_doc(client, headers, tenant_id):
    return await client.get(
        "/api/v1/documents/billing",
        params={"tenant_id": tenant_id, "collection": "runbooks"},
        headers=headers,
    )


async def _set_keystone(client, headers, tenant_id, *, title, content):
    return await client.post(
        "/api/v1/keystones",
        json={
            "tenant_id": tenant_id,
            "doc_id": f"ks-{_uid()}",
            "title": title,
            "content": content,
            "scope": "tenant",
            "weight": "high",
        },
        headers=headers,
    )


async def _keystones(client, headers, tenant_id) -> list[dict]:
    resp = await client.get(
        "/api/v1/keystones", params={"tenant_id": tenant_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- Documents -----------------------------------------------------------------


async def test_drop_refuses_a_document_and_stores_nothing(client):
    tenant_id, headers = await _tenant_with_pii_action("drop")

    resp = await _write_doc(client, headers, tenant_id, _doc_data())

    assert resp.status_code == 422, resp.text
    assert "content policy" in resp.text
    assert (await _read_doc(client, headers, tenant_id)).status_code == 404
    rows = await _pii_audit(tenant_id, "document")
    assert [r["action"] for r in rows] == ["pii_drop"]


async def test_mask_stores_a_redacted_document(client):
    tenant_id, headers = await _tenant_with_pii_action("mask")

    resp = await _write_doc(client, headers, tenant_id, _doc_data())

    assert resp.status_code == 200, resp.text
    read = await _read_doc(client, headers, tenant_id)
    assert read.status_code == 200, read.text
    stored = read.json()["data"]
    assert _EMAIL not in str(stored) and _CARD not in str(stored)
    # Each string is masked where it sits; the rest of the body is untouched.
    assert stored["contact"] == {"team": "Billing on-call", "email": "«EMAIL»"}
    assert stored["notes"] == ["Refund test card «CARD»", "Page the on-call first"]
    assert stored["summary"] == _doc_data()["summary"]
    rows = await _pii_audit(tenant_id, "document")
    assert [r["action"] for r in rows] == ["pii_mask"]
    assert set(rows[0]["detail"]["categories"]) >= {"email"}
    assert _EMAIL not in str(rows[0]["detail"])


async def test_flag_stores_the_document_as_sent_and_audits_it(client):
    tenant_id, headers = await _tenant_with_pii_action("flag")

    resp = await _write_doc(client, headers, tenant_id, _doc_data())

    assert resp.status_code == 200, resp.text
    assert (await _read_doc(client, headers, tenant_id)).json()["data"] == _doc_data()
    rows = await _pii_audit(tenant_id, "document")
    assert [r["action"] for r in rows] == ["pii_flag"]


async def test_mcp_doc_write_refused_by_drop(client, mcp_env, monkeypatch):
    tenant_id, headers = await _tenant_with_pii_action("drop")
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)

    out = await mcp_server.caura_doc(
        op="write",
        collection="runbooks",
        doc_id="billing",
        data=_doc_data(),
        agent_id="test-agent",
    )

    error = parse_envelope(out)["error"]
    assert error["code"] == "INVALID_ARGUMENTS", error
    assert "content policy" in error["message"]
    assert (await _read_doc(client, headers, tenant_id)).status_code == 404
    rows = await _pii_audit(tenant_id, "document")
    assert [r["action"] for r in rows] == ["pii_drop"]


async def test_mcp_doc_write_masked(client, mcp_env, monkeypatch):
    tenant_id, headers = await _tenant_with_pii_action("mask")
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)

    out = await mcp_server.caura_doc(
        op="write",
        collection="runbooks",
        doc_id="billing",
        data=_doc_data(),
        agent_id="test-agent",
    )

    assert parse_envelope(out).get("ok") is True, out
    stored = (await _read_doc(client, headers, tenant_id)).json()["data"]
    assert stored["contact"]["email"] == "«EMAIL»"
    assert _CARD not in str(stored)


# --- Keystones -----------------------------------------------------------------


async def test_drop_refuses_a_keystone(client):
    tenant_id, headers = await _tenant_with_pii_action("drop")

    resp = await _set_keystone(
        client,
        headers,
        tenant_id,
        title="Billing escalation",
        content=f"Escalate refunds to {_EMAIL}.",
    )

    assert resp.status_code == 422, resp.text
    assert "content policy" in resp.text
    assert await _keystones(client, headers, tenant_id) == []
    rows = await _pii_audit(tenant_id, "keystone")
    assert [r["action"] for r in rows] == ["pii_drop"]


async def test_mask_stores_a_redacted_keystone(client):
    tenant_id, headers = await _tenant_with_pii_action("mask")

    resp = await _set_keystone(
        client,
        headers,
        tenant_id,
        title=f"Escalate to {_EMAIL}",
        content=f"Refunds over the limit go to {_EMAIL}; test card {_CARD}.",
    )

    assert resp.status_code == 200, resp.text
    [rule] = await _keystones(client, headers, tenant_id)
    assert rule["data"]["title"] == "Escalate to «EMAIL»"
    assert rule["data"]["content"] == (
        "Refunds over the limit go to «EMAIL»; test card «CARD»."
    )
    # One row for the write, covering the title and the text.
    rows = await _pii_audit(tenant_id, "keystone")
    assert [r["action"] for r in rows] == ["pii_mask"]
    assert _EMAIL not in str(rows[0]["detail"])


async def test_mcp_keystone_set_masked(client, mcp_env, monkeypatch):
    tenant_id, headers = await _tenant_with_pii_action("mask")
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)

    out = await mcp_server.caura_keystones_set(
        op="set",
        doc_id=f"ks-{_uid()}",
        title="Billing escalation",
        content=f"Escalate refunds to {_EMAIL}.",
        scope="tenant",
        weight="high",
    )

    assert parse_envelope(out).get("ok") is True, out
    [rule] = await _keystones(client, headers, tenant_id)
    assert rule["data"]["content"] == "Escalate refunds to «EMAIL»."
    rows = await _pii_audit(tenant_id, "keystone")
    assert [r["action"] for r in rows] == ["pii_mask"]
