"""Write-quota policy is one decision, and both surfaces give the same answer.

The drift: a tenant sitting at its write cap was **refused** a status
transition over REST (``PATCH /memories/{id}/status`` called
``enforce_usage_limits()``) and **allowed** one over MCP
(``caura_manage(op="transition")`` checked nothing). Same operation, same
tenant, different answer depending on the transport.

The decision, recorded in ``usage_service`` as ``WRITE_QUOTA_OPS`` and
``PLAN_LIMIT_GATED_OPS``: transitions and deletes are free and ungated on both
surfaces. They are how a tenant gets back *under* its limit —
``AuthContext.enforce_usage_limits`` says exactly that about deletes, and
archiving a memory is the same action — so gating the transition contradicted
the rule it was borrowing.

Two tables rather than one boolean because the axes do not line up: ``update``
charges write budget while being gated by neither ``enforce_usage_limits()``
nor ``enforce_read_only()``.

``enforce_read_only()`` (demo mode) is neither axis and is unaffected.

Nothing here asserts the table against itself. A table nothing consults would
pass such a test while changing no behaviour, which is the failure mode these
avoid: the read-only cases run against the live route and handler, and two
tests flip a table row and assert the SURFACES react — so removing the lookup
at a call site fails them.
"""

from __future__ import annotations

import uuid

import pytest

from core_api import mcp_server
from core_api.services import usage_service
from core_api.services.usage_service import charges_write_quota, plan_limit_gated
from tests._mcp_test_helpers import is_error_envelope, stub_storage_client
from tests.conftest import get_test_auth


@pytest.fixture
def as_auth(monkeypatch):
    """Install a controlled AuthContext, mirroring the gateway header-trust path."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
                **kwargs,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _write_memory(client, tenant_id, agent_id="quota-parity"):
    headers = get_test_auth(tenant_id)[1]
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "content": f"a memory to transition {uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# The finding: REST refused what MCP allowed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_transition_is_allowed_at_the_plan_limit(client, as_auth):
    """A tenant in plan-limit read-only mode may still transition over REST.

    This is the half that was wrong. Before the fix the route called
    ``enforce_usage_limits()`` and this returned 403 — locking an over-plan
    tenant out of the archiving that would get it back under the limit.
    """
    tenant_id = f"test-tenant-quota-parity-{uuid.uuid4().hex[:8]}"
    memory_id = await _write_memory(client, tenant_id)

    as_auth(tenant_id, is_read_only=True)
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
        json={"status": "archived"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["new_status"] == "archived"


@pytest.mark.unit
async def test_mcp_transition_is_allowed_at_the_plan_limit(mcp_env, monkeypatch):
    """The other half, unchanged — pinned so parity is asserted, not assumed.

    Asserts *allowed vs refused* and that the write actually reached storage,
    deliberately not the payload shape: the shape is a separate change on its
    own PR, and this test is about the quota answer.
    """
    sc = stub_storage_client(
        monkeypatch,
        get_memory={
            "id": str(uuid.uuid4()),
            "agent_id": "alice",
            "fleet_id": None,
            "visibility": "scope_team",
            "status": "active",
        },
        update_memory_status=None,
    )

    async def _allow(*args, **kwargs):  # noqa: ARG001
        return True

    async def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(mcp_server, "authorize_memory_access", _allow)
    monkeypatch.setattr(mcp_server, "log_action", _noop)

    out = await mcp_server.caura_manage(
        op="transition", memory_id=str(uuid.uuid4()), status="archived"
    )

    assert not is_error_envelope(out)
    sc.update_memory_status.assert_awaited_once()


@pytest.mark.integration
async def test_reactivating_transition_is_allowed_at_the_plan_limit(client, as_auth):
    """Direction-independent, on purpose — the reverse transition too.

    Raised in review as a possible quota bypass: an over-plan org moving a
    memory ``archived -> active`` looks like it should be refused. It is not,
    and the reason is that the counters this service maintains have no quantity
    for it to inflate — ``tenant_usage_counters`` is keyed
    ``(tenant_id, operation, period_start)``, a per-period OPERATION count with
    no active-row dimension. A transition adds no row in either direction.

    Pinned so the permissive direction is a reviewed decision rather than a
    side-effect of treating the verb as one thing. If a plan ever meters live
    rows, this is the test that should fail and force the discussion.
    """
    tenant_id = f"test-tenant-quota-parity-{uuid.uuid4().hex[:8]}"
    memory_id = await _write_memory(client, tenant_id)
    headers = get_test_auth(tenant_id)[1]

    archived = await client.patch(
        f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
        headers=headers,
        json={"status": "archived"},
    )
    assert archived.status_code == 200, archived.text

    as_auth(tenant_id, is_read_only=True)
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
        json={"status": "active"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["old_status"] == "archived"
    assert resp.json()["new_status"] == "active"


@pytest.mark.integration
async def test_rest_transition_still_refuses_a_demo_credential(client, as_auth):
    """``enforce_read_only()`` is a different gate and must NOT have been removed.

    Dropping the plan-limit check is only correct if the demo-mode gate stays.
    Without this, "transitions are free" could be over-read into "transitions
    are ungated".
    """
    tenant_id = f"test-tenant-quota-parity-{uuid.uuid4().hex[:8]}"
    memory_id = await _write_memory(client, tenant_id)

    as_auth(tenant_id, is_demo=True)
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
        json={"status": "archived"},
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_rest_create_is_still_refused_at_the_plan_limit(client, as_auth):
    """The contrast verb: create DOES cost budget, so the limit still bites.

    Without this, removing ``enforce_usage_limits()`` everywhere would pass.
    """
    tenant_id = f"test-tenant-quota-parity-{uuid.uuid4().hex[:8]}"

    as_auth(tenant_id, is_read_only=True)
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "quota-parity",
            "content": f"should be refused {uuid.uuid4().hex}",
        },
    )

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# The table drives real call sites — flipping it changes behaviour
#
# Asserting the table against itself would be a tautology: it would pass just
# as well if nothing consulted it. These flip a row and check the SURFACES
# react, which is the property that makes the table authoritative rather than
# decorative.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_making_transition_cost_budget_takes_effect_on_rest(client, monkeypatch):
    """Add ``transition`` to the gated set and the REST route starts refusing.

    The regression this guards: the route dropping the gate via a bare
    omission, leaving the table as documentation that changes nothing.
    """
    tenant_id = f"test-tenant-quota-parity-{uuid.uuid4().hex[:8]}"
    memory_id = await _write_memory(client, tenant_id)

    monkeypatch.setattr(
        usage_service, "PLAN_LIMIT_GATED_OPS", usage_service.PLAN_LIMIT_GATED_OPS | {"transition"}
    )

    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    async def _dep():
        set_current_tenant(tenant_id)
        return AuthContext(
            tenant_id=tenant_id, agent_id=None, readable_tenant_ids=[tenant_id], is_read_only=True
        )

    app.dependency_overrides[get_auth_context] = _dep
    try:
        resp = await client.patch(
            f"/api/v1/memories/{memory_id}/status?tenant_id={tenant_id}",
            json={"status": "archived"},
        )
    finally:
        app.dependency_overrides.pop(get_auth_context, None)

    assert resp.status_code == 403, resp.text


@pytest.mark.unit
async def test_making_transition_cost_budget_takes_effect_on_mcp(mcp_env, monkeypatch):
    """Same flip, other surface: the MCP handler starts charging write budget."""
    stub_storage_client(
        monkeypatch,
        get_memory={
            "id": str(uuid.uuid4()),
            "agent_id": "alice",
            "fleet_id": None,
            "visibility": "scope_team",
            "status": "active",
        },
        update_memory_status=None,
    )

    async def _allow(*args, **kwargs):  # noqa: ARG001
        return True

    async def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(mcp_server, "authorize_memory_access", _allow)
    monkeypatch.setattr(mcp_server, "log_action", _noop)

    charged: list[tuple] = []

    async def _spy(tenant_id, operation, *a, **kw):  # noqa: ARG001
        charged.append((tenant_id, operation))

    monkeypatch.setattr(mcp_server, "check_and_increment", _spy)

    # Baseline: free, so nothing is charged.
    await mcp_server.caura_manage(
        op="transition", memory_id=str(uuid.uuid4()), status="archived"
    )
    assert charged == []

    monkeypatch.setattr(
        usage_service, "WRITE_QUOTA_OPS", usage_service.WRITE_QUOTA_OPS | {"transition"}
    )
    await mcp_server.caura_manage(
        op="transition", memory_id=str(uuid.uuid4()), status="archived"
    )
    assert charged == [(mcp_env["tenant"], "write")]


@pytest.mark.unit
def test_an_unknown_verb_raises_instead_of_defaulting():
    """A new mutating op must be a decision, not a silent default either way."""
    with pytest.raises(ValueError, match="No usage policy"):
        charges_write_quota("teleport")
    with pytest.raises(ValueError, match="No usage policy"):
        plan_limit_gated("teleport")


@pytest.mark.unit
def test_update_charges_but_is_not_plan_gated():
    """The asymmetry that makes this two tables rather than one boolean.

    ``PATCH /memories/{id}`` increments the write counter while calling neither
    ``enforce_usage_limits()`` nor ``enforce_read_only()``. Recorded as it
    BEHAVES so that wiring a call site through the table cannot silently change
    behaviour. That gap is real and tracked (#1204) — if it is closed, this
    test is the one that should fail and be updated deliberately.
    """
    assert charges_write_quota("update") is True
    assert plan_limit_gated("update") is False
