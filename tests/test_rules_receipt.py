"""A broker write names the rules delivery its session was under (g2.8).

caura-daemon stamps each memory it writes with its session's latest rules
receipt, ``metadata.rules_receipt`` (``event_id``, ``rule_set_hash``). Only the
broker's install credential is taken at its word: the receipt is kept in
``_system.rules_receipt``, where no caller can write, and anyone else's is
dropped with the platform keys.

The API tests write through the routes and read the rows back from storage;
``as_auth`` stands in for the gateway, as in ``test_low_trust_writes_held.py``.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.services.organization_settings import invalidate_cache
from core_api.services.rules_receipt import rules_receipt_from
from tests.conftest import new_tenant_id

INSTALL = "e435b91d-202b-4da4-9e21-dee620b033f8"
EVENT = "3b241101-e2bb-5255-8caf-4136c566a962"
RULES = hashlib.sha256(b"rules").hexdigest()
RECEIPT = {"event_id": EVENT, "rule_set_hash": RULES}


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


def _as_broker(as_auth, tenant: str) -> None:
    as_auth(tenant, is_install_credential=True, install_uuid=INSTALL)


async def _bulk(
    client, tenant: str, metadata: dict, *, agent_id: str | None = None
) -> str:
    body = {
        "tenant_id": tenant,
        "items": [
            {
                "content": f"the deploy window moved to thursday {uuid.uuid4().hex}",
                "metadata": metadata,
            }
        ],
    }
    if agent_id:
        body["agent_id"] = agent_id
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={"X-Bulk-Attempt-Id": f"receipt-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] == "created", result
    return result["id"]


async def _metadata(sc, tenant: str, memory_id: str) -> dict:
    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert row is not None
    return row.get("metadata_") or {}


def _kept(metadata: dict) -> dict | None:
    return (metadata.get("_system") or {}).get("rules_receipt")


# ── Through the routes ──


async def test_the_brokers_receipt_is_kept_where_no_caller_can_write(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    memory_id = await _bulk(
        client,
        tenant,
        {
            "session_id": "s-1",
            "agent_id": "claude-code",
            "rules_receipt": {"event_id": EVENT.upper(), "rule_set_hash": RULES},
        },
    )

    metadata = await _metadata(sc, tenant, memory_id)
    assert _kept(metadata) == RECEIPT
    assert "rules_receipt" not in metadata
    assert metadata["session_id"] == "s-1"


async def test_an_agent_can_not_name_its_own_receipt(client, as_auth, sc):
    tenant = new_tenant_id()
    as_auth(tenant, agent_id="agent-1")

    memory_id = await _bulk(
        client, tenant, {"rules_receipt": RECEIPT}, agent_id="agent-1"
    )

    metadata = await _metadata(sc, tenant, memory_id)
    assert _kept(metadata) is None
    assert "rules_receipt" not in metadata


async def test_a_malformed_receipt_is_dropped_and_the_write_kept(client, as_auth, sc):
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    memory_id = await _bulk(
        client,
        tenant,
        {
            "agent_id": "claude-code",
            "rules_receipt": {"event_id": "not-a-uuid", "rule_set_hash": RULES},
        },
    )

    metadata = await _metadata(sc, tenant, memory_id)
    assert _kept(metadata) is None
    assert "rules_receipt" not in metadata


async def test_only_the_bulk_route_takes_a_receipt(client, as_auth, sc):
    """The broker writes through ``/memories/bulk``; a single write strips the
    key like any platform key, whoever sends it."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "agent_id": "claude-code",
            "content": f"the deploy window moved to thursday {uuid.uuid4().hex}",
            "metadata": {"rules_receipt": RECEIPT},
        },
    )

    assert resp.status_code == 201, resp.text
    metadata = await _metadata(sc, tenant, resp.json()["id"])
    assert _kept(metadata) is None
    assert "rules_receipt" not in metadata


async def test_a_held_write_shows_its_receipt_in_the_review_queue(client, as_auth, sc):
    """g2.8's provenance for a held write: its agent, session and the receipt's
    hash, read where a person reviews it."""
    tenant = new_tenant_id()
    as_auth(tenant)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}", json={"quarantine": {"below_trust": 2}}
    )
    assert resp.status_code == 200, resp.text
    invalidate_cache(tenant)
    _as_broker(as_auth, tenant)

    memory_id = await _bulk(
        client,
        tenant,
        {"session_id": "s-held", "agent_id": "claude-code", "rules_receipt": RECEIPT},
    )

    as_auth(tenant, is_person=True, user_id="user-7", org_role="admin")
    resp = await client.get(
        f"/api/v1/memories/held?tenant_id={tenant}&session_id=s-held"
    )
    assert resp.status_code == 200, resp.text
    [held] = resp.json()["items"]
    assert held["id"] == memory_id
    assert held["status"] == QUARANTINED_MEMORY_STATUS
    assert held["system_metadata"]["hold"]["reason"] == "below_trust"
    assert held["system_metadata"]["rules_receipt"] == RECEIPT


# ── The receipt's shape ──


def test_a_receipt_is_normalised_and_keys_past_the_two_are_ignored():
    raw = {"event_id": EVENT.upper(), "rule_set_hash": RULES, "delivered_at": "later"}
    assert rules_receipt_from({"rules_receipt": raw}) == RECEIPT


def test_a_session_handed_no_rules_has_a_null_hash():
    """The broker's receipt for a delivery of nothing (no set cached) has no hash."""
    raw = {"event_id": EVENT, "rule_set_hash": None}
    assert rules_receipt_from({"rules_receipt": raw}) == raw


def test_a_write_without_a_receipt_has_none():
    assert rules_receipt_from(None) is None
    assert rules_receipt_from({"session_id": "s-1"}) is None


@pytest.mark.parametrize(
    "raw",
    [
        "a receipt",
        {"rule_set_hash": RULES},
        {"event_id": 7, "rule_set_hash": RULES},
        {"event_id": "not-a-uuid", "rule_set_hash": RULES},
        {"event_id": EVENT},
        {"event_id": EVENT, "rule_set_hash": RULES.upper()},
        {"event_id": EVENT, "rule_set_hash": RULES[:-1]},
        {"event_id": EVENT, "rule_set_hash": "z" * 64},
        {"event_id": EVENT, "rule_set_hash": 1},
    ],
)
def test_a_malformed_receipt_is_dropped_with_a_warning(raw, caplog):
    assert rules_receipt_from({"rules_receipt": raw}) is None
    assert "malformed rules receipt" in caplog.text
