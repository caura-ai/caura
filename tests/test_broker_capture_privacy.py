"""Tool output the broker captures on its own is private, scanned and marked.

caura-daemon's PostToolUse hook queues each tool call's output, redacted on the
machine, for POST /memories/bulk, tagged ``source: hook.post_tool_use``
(``queueCapture``). Nobody chose to share that output, so an install
credential's item carrying that source is treated as the plugin's captures are
(``auto_capture``): stored ``scope_agent`` whatever visibility the batch asked
for, masked by the deterministic scan even when the organization has not turned
the PII gate on, and stamped ``_system.capture`` with the broker, the trigger
and a hash of the session it came from.

Until now it was stored with the batch's visibility, ``scope_team`` by default,
and the broker searches naming no agent, which reads every shared row in the
tenant: one person's tool output was recalled into a teammate's session.

The broker's other writes in the same batch keep the batch's visibility. Anyone
else's metadata is theirs: a non-broker item tagged that way is not marked.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import new_tenant_id

INSTALL = "e435b91d-202b-4da4-9e21-dee620b033f8"
OTHER_INSTALL = "5b0f1c3e-8d2a-4c6b-9e7f-1a2b3c4d5e6f"
AGENT = "claude-code"
SESSION = "cc-sess-42"
# The first 32 hex characters of the SHA-256 of ``SESSION``
# (``auto_capture.conversation_of``).
SESSION_CONVERSATION = "d9627fb1e4569d2d193995aca9044cb4"

OUTPUT = "pytest -q on the billing service: 214 passed, 3 skipped in 41.2s"
KEY = "AKIA" + "Q" * 16


@pytest.fixture
def as_auth():
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _auth(tenant_id: str, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, readable_tenant_ids=[tenant_id], **kwargs
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _auth
    app.dependency_overrides.pop(get_auth_context, None)


def _as_broker(as_auth, tenant: str, install: str = INSTALL) -> None:
    as_auth(tenant, is_install_credential=True, install_uuid=install)


def _item(source: str, content: str = OUTPUT) -> dict:
    """An item as the broker queues it (``cloud.FormatMemoryMetadata``)."""
    return {
        "content": f"{content} {uuid.uuid4().hex}",
        "metadata": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "tool_use_id": "toolu_01XYZ",
            "tool_name": "Bash",
            "source": source,
        },
    }


def _capture(content: str = OUTPUT) -> dict:
    return _item("hook.post_tool_use", content)


async def _bulk(client, tenant: str, items: list[dict], **body) -> list[str]:
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={"tenant_id": tenant, "items": items, **body},
        headers={"X-Bulk-Attempt-Id": f"capture-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created"] * len(items), results
    return [r["id"] for r in results]


async def _row(sc, tenant: str, memory_id: str) -> dict:
    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert row is not None
    return row


def _capture_record(row: dict) -> dict | None:
    return ((row.get("metadata_") or {}).get("_system") or {}).get("capture")


@pytest.mark.integration
async def test_a_broker_capture_is_private_and_names_its_session(
    client, as_auth, sc
) -> None:
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    [memory_id] = await _bulk(client, tenant, [_capture()], visibility="scope_team")

    row = await _row(sc, tenant, memory_id)
    assert row["visibility"] == "scope_agent"
    assert row["status"] == "active"
    assert _capture_record(row) == {
        "source": "broker",
        "trigger": "post_tool_use",
        "conversation": SESSION_CONVERSATION,
    }


@pytest.mark.integration
async def test_the_brokers_other_writes_keep_the_batchs_visibility(
    client, as_auth, sc
) -> None:
    """An agent's deliberate save through the broker's MCP server travels in
    the same batches as its captures."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    captured, saved = await _bulk(
        client, tenant, [_capture(), _item("mcp.memory_save")]
    )

    assert (await _row(sc, tenant, captured))["visibility"] == "scope_agent"
    row = await _row(sc, tenant, saved)
    assert row["visibility"] == "scope_team"
    assert _capture_record(row) is None


@pytest.mark.integration
async def test_a_non_broker_item_tagged_as_a_capture_is_not_marked(
    client, as_auth, sc
) -> None:
    tenant = new_tenant_id()
    as_auth(tenant, agent_id="agent-1")

    [memory_id] = await _bulk(client, tenant, [_capture()], agent_id="agent-1")

    row = await _row(sc, tenant, memory_id)
    assert row["visibility"] == "scope_team"
    assert _capture_record(row) is None


@pytest.mark.integration
async def test_a_secret_in_a_broker_capture_is_masked_with_the_gate_off(
    client, as_auth, sc
) -> None:
    """The organization has not turned the PII gate on (the default). The
    broker's own redaction missed this one; the cloud's scan does not."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    captured, saved = await _bulk(
        client,
        tenant,
        [
            _capture(f"{OUTPUT}. The deploy key is {KEY}."),
            _item("mcp.memory_save", f"{OUTPUT}. The deploy key is {KEY}."),
        ],
    )

    assert KEY not in (await _row(sc, tenant, captured))["content"]
    # A deliberate write is governed by the organization's settings, as before.
    assert KEY in (await _row(sc, tenant, saved))["content"]


@pytest.mark.integration
async def test_another_install_does_not_recall_a_capture(client, as_auth) -> None:
    """The leak itself. Another person's broker searches naming no agent, as
    every broker does today, and no longer finds the capture; the capturing
    agent, named by the install that owns it, still does."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)
    item = _capture()
    [memory_id] = await _bulk(client, tenant, [item])

    async def _found(install: str, agent: str | None = None) -> list[str]:
        _as_broker(as_auth, tenant, install)
        named = {"caller_agent_id": agent} if agent else {}
        resp = await client.post(
            "/api/v1/search",
            json={"tenant_id": tenant, "query": item["content"], "top_k": 20, **named},
        )
        assert resp.status_code == 200, resp.text
        return [m["id"] for m in resp.json()["items"]]

    assert memory_id not in await _found(OTHER_INSTALL)
    assert memory_id in await _found(INSTALL, AGENT)
