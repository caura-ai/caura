"""Evidence contract v1 (EC-15): a broker bulk write carries the broker's evidence.

caura-daemon names the session and the tool call each capture came from in its
item metadata (``session_id``, ``tool_use_id``; PostToolUse hook). Those are what
``trace_id`` and ``tool_call_id`` mean, so a broker item that sends neither takes
them from there, instead of the per-request id every bulk batch gets. Anyone
else's metadata is theirs: a non-broker bulk item keeps the batch id.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.schemas import BulkMemoryItem
from core_api.services.memory_service import _adopt_broker_evidence
from tests.conftest import new_tenant_id

INSTALL = "e435b91d-202b-4da4-9e21-dee620b033f8"


def _item(metadata: dict | None, **fields) -> BulkMemoryItem:
    return BulkMemoryItem(content="x", metadata=metadata, **fields)


@pytest.mark.unit
def test_a_broker_item_takes_its_session_and_tool_call_from_metadata() -> None:
    item = _item({"session_id": "sess-1", "tool_use_id": "toolu_01ABC"})
    _adopt_broker_evidence(item)
    assert (item.trace_id, item.tool_call_id) == ("sess-1", "toolu_01ABC")


@pytest.mark.unit
def test_an_items_own_evidence_wins_over_its_metadata() -> None:
    item = _item(
        {"session_id": "sess-1", "tool_use_id": "toolu_01ABC"},
        trace_id="own-trace",
        tool_call_id="own-call",
    )
    _adopt_broker_evidence(item)
    assert (item.trace_id, item.tool_call_id) == ("own-trace", "own-call")


@pytest.mark.unit
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"session_id": "", "tool_use_id": ""},
        {"session_id": 7, "tool_use_id": ["x"]},
        # Values the columns would refuse are left out rather than failing the item.
        {"session_id": "has spaces", "tool_use_id": "t" * 201},
    ],
)
def test_metadata_that_names_no_usable_evidence_leaves_the_fields_unset(
    metadata,
) -> None:
    item = _item(metadata)
    _adopt_broker_evidence(item)
    assert (item.trace_id, item.tool_call_id) == (None, None)


# ── Through the route ──


@pytest.fixture
def as_auth():
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, readable_tenant_ids=[tenant_id], **kwargs
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _bulk_one(client, tenant: str, metadata: dict, **body) -> str:
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant,
            "items": [
                {
                    "content": f"the deploy window moved to thursday {uuid.uuid4().hex}",
                    "metadata": metadata,
                }
            ],
            **body,
        },
        headers={"X-Bulk-Attempt-Id": f"ec15-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] == "created", result
    return result["id"]


@pytest.mark.integration
async def test_a_broker_bulk_write_stores_the_brokers_session_and_tool_call(
    client, as_auth, sc
) -> None:
    tenant = new_tenant_id()
    as_auth(tenant, is_install_credential=True, install_uuid=INSTALL)

    memory_id = await _bulk_one(
        client,
        tenant,
        {
            "session_id": "cc-sess-42",
            "tool_use_id": "toolu_01XYZ",
            "agent_id": "claude-code",
        },
    )

    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert (row["trace_id"], row["tool_call_id"]) == ("cc-sess-42", "toolu_01XYZ")


@pytest.mark.integration
async def test_a_non_broker_bulk_write_keeps_the_batch_trace_id(
    client, as_auth, sc
) -> None:
    tenant = new_tenant_id()
    as_auth(tenant, agent_id="agent-1")

    memory_id = await _bulk_one(
        client,
        tenant,
        {"session_id": "not-a-broker", "tool_use_id": "toolu_01XYZ"},
        agent_id="agent-1",
    )

    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert row["trace_id"].startswith("req-")
    assert row["tool_call_id"] is None
