"""Unit tests for ``memclaw_keystones`` and ``memclaw_keystones_set`` (CAURA-000).

Covers:
- Read: auth, payload shape, truncation flag pass-through, fleet/agent scoping.
- Write/delete: op validation, trust gate, payload pass-through, error envelopes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope, strip_latency

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_storage_client(monkeypatch, **method_returns):
    """Replace ``get_storage_client`` with a MagicMock whose methods return
    the requested values. Each kwarg is the method name (e.g. ``list_keystones``)
    and the value is the awaited result.
    """
    sc = MagicMock(name="storage_client")
    for name, ret in method_returns.items():
        setattr(sc, name, AsyncMock(return_value=ret))

    def _factory():
        return sc

    # The handler binds ``get_storage_client`` at module import time, so
    # the test must patch the alias on ``mcp_server`` (where Python
    # resolves it at call time) — not the original module path.
    monkeypatch.setattr("core_api.mcp_server.get_storage_client", _factory)
    return sc


# ---------------------------------------------------------------------------
# memclaw_keystones (read)
# ---------------------------------------------------------------------------


async def test_keystones_read_returns_rules(mcp_env, monkeypatch):
    rows = [
        {"doc_id": "no-secrets", "data": {"scope": "tenant", "weight": 100}},
        {"doc_id": "use-feature-x", "data": {"scope": "fleet", "weight": 50}},
    ]
    _stub_storage_client(monkeypatch, list_keystones=(rows, False))
    out = await mcp_server.memclaw_keystones(fleet_id="fleet-A")
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert payload["truncated"] is False
    assert [r["doc_id"] for r in payload["rules"]] == ["no-secrets", "use-feature-x"]


async def test_keystones_read_propagates_truncation(mcp_env, monkeypatch):
    _stub_storage_client(monkeypatch, list_keystones=([{"doc_id": "a"}], True))
    out = await mcp_server.memclaw_keystones(fleet_id="fleet-A")
    assert parse_envelope(out)["truncated"] is True


async def test_keystones_read_drops_agent_id_when_no_fleet(mcp_env, monkeypatch):
    """agent_id without fleet_id can't resolve agent-scope rows; the handler
    must NOT forward agent_id under that shape (would silently miss them at
    the storage layer anyway, but defence in depth)."""
    sc = _stub_storage_client(monkeypatch, list_keystones=([], False))
    await mcp_server.memclaw_keystones(agent_id="agent-Z", fleet_id=None)
    sc.list_keystones.assert_awaited_once()
    kwargs = sc.list_keystones.await_args.kwargs
    assert kwargs["agent_id"] is None
    assert kwargs["fleet_id"] is None


# ---------------------------------------------------------------------------
# memclaw_keystones_set (write/delete)
# ---------------------------------------------------------------------------


async def test_keystones_set_unknown_op(mcp_env):
    out = await mcp_server.memclaw_keystones_set(op="oops", doc_id="x")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "set|delete" in payload["error"]["message"]


async def test_keystones_set_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_keystones_set(op="set", doc_id="")
    assert "doc_id is required" in strip_latency(out)


async def test_keystones_set_trust_denied(mcp_env, monkeypatch):
    """Low-trust agent must be rejected — keystones override user instructions,
    so a compromised agent cannot be allowed to plant one."""

    async def _deny(db, tenant_id, agent_id, min_level):
        return 0, False, "INSUFFICIENT_TRUST level 1 required"

    monkeypatch.setattr(mcp_server, "_require_trust", _deny)
    # ``op=set`` now fetches the existing rule before the gate so the
    # effective floor can combine new + stored shapes. ``None`` means
    # "no existing rule, this is a create".
    _stub_storage_client(monkeypatch, get_document=None)
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="no-secrets",
        title="No secrets",
        content="Never commit credentials.",
        scope="tenant",
        weight="high",
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_keystones_set_happy_path(mcp_env, monkeypatch):
    # Use a real UUID — audit_logs' resource_id validator (storage side)
    # rejects non-UUID strings with 422 and would mask the happy-path
    # assertion below.
    sc = _stub_storage_client(
        monkeypatch,
        get_document=None,  # fresh create — no existing rule
        upsert_keystone={
            "id": "11111111-1111-4111-8111-111111111111",
            "doc_id": "no-secrets",
        },
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="no-secrets",
        title="No secrets",
        content="Never commit credentials.",
        scope="tenant",
        weight="high",
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["action"] == "set"
    assert payload["doc_id"] == "no-secrets"
    # Storage was invoked once with the full payload — scope/weight/etc passed through.
    sc.upsert_keystone.assert_awaited_once()
    sent = sc.upsert_keystone.await_args.args[0]
    assert sent["scope"] == "tenant"
    assert sent["weight"] == "high"
    assert sent["doc_id"] == "no-secrets"


async def test_keystones_delete_happy_path(mcp_env, monkeypatch):
    # ``op=delete`` now fetches the rule first to learn its scope (so
    # the trust gate can compute ``min_level``). Provide a tenant-scope
    # stub so the gate resolves to the ≥ 2 path the default mcp_env
    # mock allows.
    _stub_storage_client(
        monkeypatch,
        get_document={"data": {"scope": "tenant"}},
        delete_keystone=True,
    )
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="no-secrets")
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["action"] == "delete"


async def test_keystones_delete_not_found(mcp_env, monkeypatch):
    # The pre-lookup returns None now, so ``not found`` lands BEFORE
    # the delete call instead of after — same envelope.
    _stub_storage_client(monkeypatch, get_document=None)
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="ghost")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tiered trust (agent self-author at ≥ 1; fleet/tenant/cross-agent at ≥ 2)
# ---------------------------------------------------------------------------


def _capture_trust(monkeypatch, allow: bool = True, trust_level: int = 3):
    """Replace ``_require_trust`` with a probe that records the
    ``min_level`` it was called with and returns a configurable
    ``trust_level``. The probe lets tests assert BOTH which floor the
    handler explicitly asked the trust service for, and (via the
    returned trust level) which in-memory floor checks the handler
    then enforces against the cached value — the delete path now does
    one ``_require_trust`` call followed by an in-memory comparison.
    """
    calls: list[int] = []

    async def _probe(db, tenant_id, agent_id, min_level):
        calls.append(min_level)
        if allow:
            return trust_level, False, None
        return 1, False, "Error (403): INSUFFICIENT_TRUST"

    monkeypatch.setattr(mcp_server, "_require_trust", _probe)
    return calls


async def test_set_agent_scope_self_succeeds_at_trust_1(mcp_env, monkeypatch):
    """A trust-1 caller can author a self-owned ``scope=agent`` rule.
    The set path now does ONE ``_require_trust`` call (min_level=1, the
    anti-probing floor) and compares the returned trust level against
    the scope-derived floor in-memory — matching the delete pattern."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document=None,  # create — no stored shape competes with new
        upsert_keystone={"id": "11111111-1111-4111-8111-111111111111", "doc_id": "r"},
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="r",
        title="T",
        content="C",
        scope="agent",
        weight="med",
        fleet_id="fleet-X",
        agent_id="agent-A",
    )
    assert parse_envelope(out)["ok"] is True
    # One DB round-trip — the rule-shape floor (=1 for self-author) is
    # checked in-memory against the returned trust level.
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_set_agent_scope_other_rejected_at_trust_1(mcp_env, monkeypatch):
    """A trust-1 caller CANNOT author a ``scope=agent`` rule targeting
    a different agent (admin-on-behalf). The single ``_require_trust``
    call passes the anti-probing floor (≥ 1) but the in-memory
    comparison against the scope-derived floor (=2 for cross-agent)
    catches it."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document=None,
        upsert_keystone={"id": "11111111-1111-4111-8111-111111111111", "doc_id": "r"},
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="r",
        title="T",
        content="C",
        scope="agent",
        weight="med",
        fleet_id="fleet-X",
        agent_id="agent-OTHER",
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_set_fleet_scope_rejected_at_trust_1(mcp_env, monkeypatch):
    """``scope=fleet`` stays at the cross-agent governance tier (≥ 2),
    so a trust-1 caller is rejected by the in-memory floor check."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document=None,
        upsert_keystone={"id": "11111111-1111-4111-8111-111111111111", "doc_id": "r"},
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="r",
        title="T",
        content="C",
        scope="fleet",
        weight="med",
        fleet_id="fleet-X",
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_delete_own_agent_rule_succeeds_at_trust_1(mcp_env, monkeypatch):
    """A trust-1 caller can delete its own ``scope=agent`` rule. The
    delete path now does ONE ``_require_trust`` call (min_level=1, the
    anti-probing floor) and compares the returned trust level against
    the scope-derived floor in-memory. With trust=1 returned and the
    rule's own-agent floor=1, the delete proceeds."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document={"data": {"scope": "agent", "agent_id": "agent-A"}},
        delete_keystone=True,
    )
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="r")
    assert parse_envelope(out)["ok"] is True
    # One DB round-trip (was two pre-consolidation). The explicit
    # min_level is the anti-probing floor (1); the rule-shape floor is
    # checked in-memory against the returned trust level.
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_set_overwrite_fleet_with_self_agent_uses_stored_floor(
    mcp_env, monkeypatch
):
    """Privilege-escalation guard: when ``op=set`` targets a doc_id
    that already exists as ``scope=fleet``, the trust floor is the
    max of (new-shape floor, stored-shape floor). A trust-1 attacker
    submitting ``scope=agent``+``agent_id=<self>`` would otherwise
    pass at level 1 and silently overwrite a fleet rule. With the
    single-DB-call pattern, the in-memory check catches it."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document={"data": {"scope": "fleet"}},
        upsert_keystone={"id": "11111111-1111-4111-8111-111111111111", "doc_id": "r"},
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="r",
        title="T",
        content="C",
        scope="agent",
        weight="med",
        fleet_id="fleet-X",
        agent_id="agent-A",  # self — would be the trust-1 path on its own
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    # ``effective_keystone_min_trust`` resolved to 2 (stored fleet
    # wins); the in-memory ``trust < min_level`` comparison catches
    # the trust-1 attempt after a single round-trip.
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_delete_fleet_rule_rejected_at_trust_1(mcp_env, monkeypatch):
    """A trust-1 caller CANNOT delete a ``scope=fleet`` rule even
    though it clears the anti-probing floor (≥ 1). The in-memory
    comparison against the scope-derived floor (=2 for fleet) catches
    it after the single ``_require_trust`` round-trip."""
    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    trust_calls = _capture_trust(monkeypatch, allow=True, trust_level=1)
    _stub_storage_client(
        monkeypatch,
        get_document={"data": {"scope": "fleet"}},
        delete_keystone=True,
    )
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="r")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    # Still one DB round-trip — the floor failure is detected in-memory
    # against the cached trust level.
    assert trust_calls == [1], f"expected single [1] DB call, got {trust_calls}"


async def test_set_aborts_when_stored_scope_changes_between_reads(
    mcp_env, monkeypatch
):
    """TOCTOU narrowing on upsert: if a concurrent upsert promotes the
    stored row's scope between the gate read and the recheck immediately
    before the write, the handler must return a CONFLICT envelope and
    skip the upsert. Mirrors the same protection on delete."""
    from unittest.mock import AsyncMock, MagicMock

    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    _capture_trust(monkeypatch, allow=True, trust_level=2)

    sc = MagicMock(name="storage_client")
    # Gate read sees a self-owned agent rule (caller authorised); recheck
    # right before the write sees a fleet rule (now requires ≥ 2; even
    # if the caller has trust 2, the rule shape changed under their feet
    # so abort to preserve the audit story).
    sc.get_document = AsyncMock(
        side_effect=[
            {"data": {"scope": "agent", "agent_id": "agent-A"}},
            {"data": {"scope": "fleet", "agent_id": None}},
        ]
    )
    sc.upsert_keystone = AsyncMock(
        return_value={"id": "11111111-1111-4111-8111-111111111111", "doc_id": "r"}
    )
    monkeypatch.setattr("core_api.mcp_server.get_storage_client", lambda: sc)

    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="r",
        title="T",
        content="C",
        scope="agent",
        weight="med",
        fleet_id="fleet-X",
        agent_id="agent-A",
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "CONFLICT", payload
    sc.upsert_keystone.assert_not_awaited()


async def test_delete_aborts_when_stored_scope_changes_between_reads(
    mcp_env, monkeypatch
):
    """TOCTOU narrowing: if a concurrent upsert promotes a rule's scope
    between the trust-gate read and the recheck immediately before the
    delete, the handler must abort with a CONFLICT envelope rather than
    proceed to delete a row whose authorisation floor has shifted."""
    from unittest.mock import AsyncMock, MagicMock

    mcp_env["monkeypatch"].setattr(mcp_server, "_get_agent_id", lambda: "agent-A")
    _capture_trust(monkeypatch, allow=True, trust_level=1)

    sc = MagicMock(name="storage_client")
    # First ``get_document`` (gate read): the rule is self-owned agent
    # scope — trust-1 caller is authorised. Second ``get_document``
    # (recheck): a concurrent upsert has promoted it to ``scope=fleet``,
    # which would now require trust ≥ 2. The handler must refuse.
    sc.get_document = AsyncMock(
        side_effect=[
            {"data": {"scope": "agent", "agent_id": "agent-A"}},
            {"data": {"scope": "fleet", "agent_id": None}},
        ]
    )
    sc.delete_keystone = AsyncMock(return_value=True)
    monkeypatch.setattr("core_api.mcp_server.get_storage_client", lambda: sc)

    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="r")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "CONFLICT", payload
    # Delete must NOT have fired against the storage layer.
    sc.delete_keystone.assert_not_awaited()
