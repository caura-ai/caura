"""A write the broker's gate refused is held for review, with its provenance (g2.5).

caura-daemon's write gate refuses an agent's write to a memory file when the
fleet policy denies it or requires approval, and sends the refused write here
to be held: a bulk item with ``status: "quarantined"``, its content what the
agent tried to put in the file, and the gate's account of it in
``metadata.write_gate``. Only the broker's install credential may ask for that
status. The account is kept in ``_system.hold`` (reason ``write_gate``), beside
the session's rules receipt, and the review queue shows both.

``as_auth`` stands in for the gateway, as in ``test_rules_receipt.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.services.organization_settings import invalidate_cache
from core_api.services.write_gate_hold import write_gate_hold_from
from tests.conftest import new_tenant_id

INSTALL = "e435b91d-202b-4da4-9e21-dee620b033f8"
RECEIPT = {
    "event_id": "3b241101-e2bb-5255-8caf-4136c566a962",
    "rule_set_hash": hashlib.sha256(b"rules").hexdigest(),
}
ACCOUNT = {
    "tool": "Edit",
    "paths": ["/home/me/repo/CLAUDE.md"],
    "action": "require_approval",
    "rule_ids": ["memory-files"],
}
HOLD = {"reason": "write_gate", **ACCOUNT}


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


def _refused_write(session: str = "s-gate", **metadata) -> dict:
    """An item as the broker sends a write its gate refused."""
    return {
        "content": f"use pnpm, not npm, in this repo {uuid.uuid4().hex}",
        "status": QUARANTINED_MEMORY_STATUS,
        "metadata": {
            "session_id": session,
            "agent_id": "claude-code",
            "rules_receipt": RECEIPT,
            **metadata,
        },
    }


async def _bulk(client, tenant: str, *items: dict) -> list[dict]:
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={"tenant_id": tenant, "items": list(items)},
        headers={"X-Bulk-Attempt-Id": f"gate-{uuid.uuid4().hex}"},
    )
    assert resp.status_code in (200, 207), resp.text
    return resp.json()["results"]


async def _row(sc, tenant: str, memory_id: str) -> dict:
    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert row is not None
    return row


def _system(row: dict) -> dict:
    return (row.get("metadata_") or {}).get("_system") or {}


# ── Through the routes ──


async def test_a_refused_write_is_held_with_the_gates_account(client, as_auth, sc):
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)

    [result] = await _bulk(client, tenant, _refused_write(write_gate=ACCOUNT))

    assert result["status"] == "created", result
    row = await _row(sc, tenant, result["id"])
    assert row["status"] == QUARANTINED_MEMORY_STATUS
    assert _system(row)["hold"] == HOLD
    assert _system(row)["rules_receipt"] == RECEIPT
    # The account is kept where no caller writes, not left in the caller's keys.
    assert "write_gate" not in row["metadata_"]
    assert row["metadata_"]["session_id"] == "s-gate"


async def test_the_review_queue_shows_a_held_writes_provenance(client, as_auth, sc):
    """The row's done criterion: the held write, read where a person reviews it,
    names its file, its proposed content, its session, its agent and the hash of
    the rules the session was delivered."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)
    item = _refused_write(session="s-review", write_gate=ACCOUNT)
    [result] = await _bulk(client, tenant, item)

    as_auth(tenant, is_person=True, user_id="user-7", org_role="admin")
    resp = await client.get(
        f"/api/v1/memories/held?tenant_id={tenant}&session_id=s-review"
    )

    assert resp.status_code == 200, resp.text
    [held] = resp.json()["items"]
    assert held["id"] == result["id"]
    assert held["status"] == QUARANTINED_MEMORY_STATUS
    assert held["content"] == item["content"]
    assert held["agent_id"] == "claude-code"
    assert held["metadata"]["session_id"] == "s-review"
    assert held["system_metadata"]["hold"] == HOLD
    assert held["system_metadata"]["rules_receipt"] == RECEIPT


async def test_a_released_held_write_goes_live(client, as_auth, sc):
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)
    [result] = await _bulk(client, tenant, _refused_write(write_gate=ACCOUNT))

    as_auth(tenant, is_person=True, user_id="user-7", org_role="admin")
    resp = await client.patch(
        f"/api/v1/memories/{result['id']}/status?tenant_id={tenant}",
        json={"status": "active"},
    )

    assert resp.status_code == 200, resp.text
    assert (await _row(sc, tenant, result["id"]))["status"] == "active"


async def test_the_weeks_counts_see_held_writes_a_person_has_decided(
    client, as_auth, sc
):
    """The pilot report's numbers (g4.3): a release and a reject leave the hold
    on the write, so the week still counts both as held, and each decision is
    counted from the audit row it left."""
    tenant = new_tenant_id()
    since = datetime.now(UTC) - timedelta(hours=1)
    _as_broker(as_auth, tenant)
    released, rejected = await _bulk(
        client,
        tenant,
        _refused_write(write_gate=ACCOUNT),
        _refused_write(write_gate=ACCOUNT),
    )

    as_auth(tenant, is_person=True, user_id="user-7", org_role="admin")
    for result, status in ((released, "active"), (rejected, "cancelled")):
        resp = await client.patch(
            f"/api/v1/memories/{result['id']}/status?tenant_id={tenant}",
            json={"status": status},
        )
        assert resp.status_code == 200, resp.text
    # The release replays what the held write skipped. Its enrichment then
    # writes into ``_system`` (no model runs here, so the test writes as it does).
    from core_api.tasks import _background_tasks

    pending = [task for task in _background_tasks if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=30)
    await sc.update_memory(
        released["id"],
        tenant,
        {"metadata_patch": {"_system": {"enrichment_pending": False}}},
    )
    counts = await sc._get(
        "/memories/held/counts",
        tenant_id=tenant,
        since=since.isoformat(),
        until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )

    assert _system(await _row(sc, tenant, released["id"]))["hold"] == HOLD
    assert counts == {
        "held": {"write_gate": 2},
        "released": 1,
        "rejected": 1,
        "rolled_back": 0,
    }


async def test_only_the_broker_may_ask_for_a_hold(client, as_auth, sc):
    tenant = new_tenant_id()
    as_auth(tenant, agent_id="agent-1")

    [result] = await _bulk(
        client, tenant, {**_refused_write(write_gate=ACCOUNT), "agent_id": "agent-1"}
    )

    assert result["status"] == "error", result
    assert "status must be one of" in result["error"]
    assert result.get("id") is None


async def test_a_writers_own_account_is_dropped(client, as_auth, sc):
    """From anyone but the broker the account is a platform key, stripped like
    any: a write can't make up its own provenance."""
    tenant = new_tenant_id()
    as_auth(tenant, agent_id="agent-1")
    item = _refused_write(write_gate=ACCOUNT)
    del item["status"]

    [result] = await _bulk(client, tenant, item)

    assert result["status"] == "created", result
    row = await _row(sc, tenant, result["id"])
    assert row["status"] == "active"
    assert "write_gate" not in row["metadata_"]
    assert "hold" not in _system(row)


async def test_a_malformed_account_still_holds_the_write(client, as_auth, sc):
    """The status holds the write; a broker bug in the account loses the
    account, not the hold."""
    tenant = new_tenant_id()
    _as_broker(as_auth, tenant)
    account = {"tool": "", "paths": "CLAUDE.md", "action": "approve", "rule_ids": [7]}

    [result] = await _bulk(client, tenant, _refused_write(write_gate=account))

    row = await _row(sc, tenant, result["id"])
    assert row["status"] == QUARANTINED_MEMORY_STATUS
    assert _system(row)["hold"] == {"reason": "write_gate"}


async def test_the_gates_account_names_the_hold_where_trust_holds_the_batch(
    client, as_auth, sc
):
    """A broker whose agent is below the organization's trust level has every
    write held; the one its gate refused says so, and the rest say why they are
    held."""
    tenant = new_tenant_id()
    as_auth(tenant)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}", json={"quarantine": {"below_trust": 2}}
    )
    assert resp.status_code == 200, resp.text
    invalidate_cache(tenant)
    _as_broker(as_auth, tenant)
    plain = _refused_write()
    del plain["status"]

    refused, captured = await _bulk(
        client, tenant, _refused_write(write_gate=ACCOUNT), plain
    )

    assert _system(await _row(sc, tenant, refused["id"]))["hold"] == HOLD
    assert (
        _system(await _row(sc, tenant, captured["id"]))["hold"]["reason"]
        == "below_trust"
    )


async def test_the_gates_account_survives_the_batch_being_decided_again(
    client, as_auth, sc
):
    """The trust level is raised by another process, so storage refuses the
    batch this one decided live and it is decided again (#1984): the write the
    gate refused still names the gate, and the rest are held for trust."""
    from core_api.services.organization_settings import resolve_config

    tenant = new_tenant_id()
    await resolve_config(tenant)  # this process caches no level
    await sc.update_org_settings(tenant, {"quarantine": {"below_trust": 2}})
    _as_broker(as_auth, tenant)
    plain = _refused_write()
    del plain["status"]

    refused, captured = await _bulk(
        client, tenant, _refused_write(write_gate=ACCOUNT), plain
    )

    assert _system(await _row(sc, tenant, refused["id"]))["hold"] == HOLD
    assert (
        _system(await _row(sc, tenant, captured["id"]))["hold"]["reason"]
        == "below_trust"
    )


# ── The account's shape ──


def test_an_account_is_kept_and_keys_past_the_four_are_ignored():
    raw = {**ACCOUNT, "diff": "-a\n+b"}
    assert write_gate_hold_from({"write_gate": raw}) == HOLD


def test_a_write_with_no_account_is_held_for_the_gate_all_the_same():
    assert write_gate_hold_from(None) == {"reason": "write_gate"}
    assert write_gate_hold_from({"session_id": "s-1"}) == {"reason": "write_gate"}


@pytest.mark.parametrize(
    ("account", "kept"),
    [
        ("an account", {}),
        ({**ACCOUNT, "tool": 7}, {k: v for k, v in ACCOUNT.items() if k != "tool"}),
        (
            {**ACCOUNT, "tool": "x" * 65},
            {k: v for k, v in ACCOUNT.items() if k != "tool"},
        ),
        (
            {**ACCOUNT, "paths": "CLAUDE.md"},
            {k: v for k, v in ACCOUNT.items() if k != "paths"},
        ),
        ({**ACCOUNT, "paths": []}, {k: v for k, v in ACCOUNT.items() if k != "paths"}),
        (
            {**ACCOUNT, "paths": ["a"] * 17},
            {k: v for k, v in ACCOUNT.items() if k != "paths"},
        ),
        (
            {**ACCOUNT, "paths": ["x" * 4097]},
            {k: v for k, v in ACCOUNT.items() if k != "paths"},
        ),
        (
            {**ACCOUNT, "rule_ids": [""]},
            {k: v for k, v in ACCOUNT.items() if k != "rule_ids"},
        ),
        (
            {**ACCOUNT, "action": "allow"},
            {k: v for k, v in ACCOUNT.items() if k != "action"},
        ),
    ],
)
def test_a_malformed_part_is_dropped_with_a_warning(account, kept, caplog):
    assert write_gate_hold_from({"write_gate": account}) == {
        "reason": "write_gate",
        **kept,
    }
    assert "write-gate" in caplog.text


def test_the_gate_holds_on_deny_too():
    assert (
        write_gate_hold_from({"write_gate": {**ACCOUNT, "action": "deny"}})["action"]
        == "deny"
    )
