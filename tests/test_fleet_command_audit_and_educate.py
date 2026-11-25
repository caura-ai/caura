"""Queued fleet commands are attributed, and ``educate`` needs an org admin.

* ``POST /fleet/commands`` writes an audit row for every command it queues,
  naming the node and the command. The actor comes from the request's audit
  principal, which ``log_action`` attaches. The payload is not copied into the
  row: an ``educate`` prompt can be 64 KB of free text.
* ``educate`` appends its prompt to the HEARTBEAT.md of every agent workspace
  on the node, a standing instruction to those agents. Like custom plugin
  ``source``, only an org admin may queue it.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import fleet

pytestmark = pytest.mark.unit

NODE = "11111111-2222-3333-4444-555555555555"
COMMAND_ID = "99999999-8888-7777-6666-555555555555"

TENANT_KEY = AuthContext(tenant_id="t1")
ORG_ADMIN = AuthContext(tenant_id="t1", org_role="admin")
OPERATOR = AuthContext(tenant_id=None, is_admin=True)


class _Storage:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_command(self, data: dict) -> dict:
        self.created.append(data)
        return {"id": COMMAND_ID, "status": "pending"}


@pytest.fixture
def storage(monkeypatch) -> _Storage:
    sc = _Storage()
    monkeypatch.setattr(fleet, "get_storage_client", lambda: sc)
    return sc


@pytest.fixture
def audit(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _log_action(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fleet, "log_action", _log_action)
    return calls


def _body(command: str, payload: dict | None = None) -> fleet.CommandIn:
    return fleet.CommandIn(
        tenant_id="t1", node_id=NODE, command=command, payload=payload
    )


# ── audit row ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("ping", None),
        ("restart", {}),
        ("update_plugin", {}),
        ("educate", {"prompt": "be kind"}),
    ],
)
async def test_every_queued_command_writes_one_audit_row(
    storage, audit, command, payload
):
    await fleet.create_command(_body(command, payload), ORG_ADMIN)

    assert len(storage.created) == 1
    assert len(audit) == 1
    row = audit[0]
    assert row["tenant_id"] == "t1"
    assert row["resource_type"] == "fleet_command"
    assert str(row["resource_id"]) == COMMAND_ID
    assert row["detail"]["node_id"] == NODE
    assert row["detail"]["command"] == command


async def test_the_audit_row_does_not_copy_the_payload(storage, audit):
    await fleet.create_command(
        _body("educate", {"prompt": "secret instructions"}), ORG_ADMIN
    )

    assert "secret instructions" not in repr(audit[0])


async def test_a_refused_command_writes_no_audit_row(storage, audit):
    with pytest.raises(HTTPException):
        await fleet.create_command(_body("deploy", {"source": "evil()"}), TENANT_KEY)

    assert storage.created == []
    assert audit == []


# ── educate gate ────────────────────────────────────────────────────


async def test_tenant_key_cannot_queue_educate(storage, audit):
    with pytest.raises(HTTPException) as exc:
        await fleet.create_command(_body("educate", {"prompt": "do this"}), TENANT_KEY)

    assert exc.value.status_code == 403
    assert storage.created == []


@pytest.mark.parametrize("auth", [ORG_ADMIN, OPERATOR], ids=["org-admin", "operator"])
async def test_org_admin_may_queue_educate(storage, audit, auth):
    created = await fleet.create_command(_body("educate", {"prompt": "do this"}), auth)

    assert created == {"id": COMMAND_ID, "status": "pending"}


@pytest.mark.parametrize("command", ["ping", "restart", "update_plugin"])
async def test_tenant_key_keeps_its_other_commands(storage, audit, command):
    created = await fleet.create_command(_body(command, {}), TENANT_KEY)

    assert created["status"] == "pending"
