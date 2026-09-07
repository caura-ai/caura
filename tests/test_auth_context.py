"""Unit tests for AuthContext enforcement methods."""

from __future__ import annotations

import pytest

# C32 / API-05: ``detail`` on an auth refusal is now
# ``{"code", "message", "details"}`` so the eight distinct reasons this boundary
# can refuse a request stop collapsing into the single status-derived
# ``FORBIDDEN``. The human message is unchanged and lives under ``["message"]``;
# these assertions follow it there, and additionally pin the code — which is the
# part a caller is now expected to branch on.
from fastapi import HTTPException

from core_api import errors
from core_api.auth import AuthContext


def test_enforce_read_only_allows_non_demo():
    ctx = AuthContext(tenant_id="t1", is_demo=False)
    ctx.enforce_read_only()  # no raise


def test_enforce_read_only_blocks_demo():
    ctx = AuthContext(tenant_id="t1", is_demo=True)
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_read_only()
    assert exc_info.value.status_code == 403
    assert "demo" in exc_info.value.detail["message"].lower()
    assert exc_info.value.detail["code"] == errors.AUTH_DEMO_SANDBOX


def test_enforce_usage_limits_allows_normal_org():
    ctx = AuthContext(tenant_id="t1", is_read_only=False)
    ctx.enforce_usage_limits()  # no raise


def test_enforce_usage_limits_blocks_read_only_org():
    ctx = AuthContext(tenant_id="t1", is_read_only=True)
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_usage_limits()
    assert exc_info.value.status_code == 403
    assert "read-only" in exc_info.value.detail["message"].lower()
    assert "upgrade" in exc_info.value.detail["message"].lower()
    assert exc_info.value.detail["code"] == errors.AUTH_PLAN_LIMIT


def test_read_only_is_independent_of_demo():
    """is_demo and is_read_only are separate flags enforced by separate methods."""
    # Demo but not read-only
    ctx = AuthContext(tenant_id="t1", is_demo=True, is_read_only=False)
    with pytest.raises(HTTPException):
        ctx.enforce_read_only()
    ctx.enforce_usage_limits()  # not read-only → no raise

    # Read-only but not demo (post-cancellation over limits)
    ctx = AuthContext(tenant_id="t1", is_demo=False, is_read_only=True)
    ctx.enforce_read_only()  # not demo → no raise
    with pytest.raises(HTTPException):
        ctx.enforce_usage_limits()


# ── readable_tenant_ids defaults ─────────────────────────────────────


def test_readable_tenant_ids_defaults_to_home_tenant():
    ctx = AuthContext(tenant_id="t1")
    assert ctx.readable_tenant_ids == ["t1"]
    assert ctx.is_cross_tenant_read is False


def test_readable_tenant_ids_empty_when_tenant_is_none():
    ctx = AuthContext(tenant_id=None, is_admin=True)
    assert ctx.readable_tenant_ids == []


def test_readable_tenant_ids_prepends_home_tenant_if_missing():
    ctx = AuthContext(tenant_id="home", readable_tenant_ids=["other-a", "other-b"])
    assert ctx.readable_tenant_ids == ["home", "other-a", "other-b"]
    assert ctx.is_cross_tenant_read is True


def test_readable_tenant_ids_keeps_explicit_home_position():
    ctx = AuthContext(tenant_id="home", readable_tenant_ids=["home", "other-a"])
    assert ctx.readable_tenant_ids == ["home", "other-a"]


# ── enforce_readable_tenant ──────────────────────────────────────────


def test_enforce_readable_tenant_allows_home_tenant():
    ctx = AuthContext(tenant_id="t1")
    ctx.enforce_readable_tenant("t1")  # no raise


def test_enforce_readable_tenant_allows_widened_tenant():
    ctx = AuthContext(tenant_id="home", readable_tenant_ids=["other-a"])
    ctx.enforce_readable_tenant("home")
    ctx.enforce_readable_tenant("other-a")


def test_enforce_readable_tenant_blocks_unrelated_tenant():
    ctx = AuthContext(tenant_id="home", readable_tenant_ids=["other-a"])
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_readable_tenant("intruder")
    assert exc_info.value.status_code == 403


def test_enforce_readable_tenant_admin_bypass():
    ctx = AuthContext(tenant_id=None, is_admin=True)
    ctx.enforce_readable_tenant("anything")  # no raise


# ── enforce_write_scope ──────────────────────────────────────────────


def test_enforce_write_scope_noop_when_scopes_unset():
    ctx = AuthContext(tenant_id="t1")
    ctx.enforce_write_scope()  # no raise — legacy/full-scope path


def test_enforce_write_scope_allows_when_write_in_scopes():
    ctx = AuthContext(tenant_id="t1", scopes={"recall", "search", "write"})
    ctx.enforce_write_scope()


def test_enforce_write_scope_blocks_read_only_scopes():
    ctx = AuthContext(
        tenant_id="t1",
        scopes={"recall", "search", "memories_read", "documents_read"},
    )
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_write_scope()
    assert exc_info.value.status_code == 403
    assert "read-only" in exc_info.value.detail["message"].lower()
    assert exc_info.value.detail["code"] == errors.AUTH_READ_ONLY_KEY


# ── enforce_read_only also enforces scope (composite gate) ───────────


def test_enforce_read_only_blocks_scope_restricted_keys():
    """The write-side aggregate gate at every endpoint head — demo +
    scope are both checked in one call so existing handlers don't need
    per-site changes to honor read-only cross-tenant credentials
    (kind=cross_tenant with the ``write`` capability omitted)."""
    ctx = AuthContext(
        tenant_id="t1",
        scopes={"recall", "search", "memories_read", "documents_read"},
    )
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_read_only()
    assert exc_info.value.status_code == 403
    assert "read-only" in exc_info.value.detail["message"].lower()
    assert exc_info.value.detail["code"] == errors.AUTH_READ_ONLY_KEY


def test_enforce_read_only_passes_when_write_in_scopes():
    ctx = AuthContext(tenant_id="t1", scopes={"recall", "write"})
    ctx.enforce_read_only()  # no raise


def test_enforce_read_only_passes_when_scopes_unset():
    """Legacy / full-scope keys (scopes=None) must still pass — this
    is the most common path."""
    ctx = AuthContext(tenant_id="t1")
    ctx.enforce_read_only()  # no raise


# ── enforce_self_agent ───────────────────────────────────────────────
#
# The self plane. Unlike its neighbours here the refusal carries a plain-string
# ``detail`` rather than the C32 ``{"code", "message", "details"}`` shape, so
# these assertions read ``detail`` directly — that is what all eight call sites
# raised before the condition moved onto the helper, and changing the body for
# existing clients is not something a consolidation should do quietly.


def test_enforce_self_agent_noop_without_an_agent_credential():
    """A tenant/user/admin credential carries no agent identity, so the self
    plane has nothing to compare and must not narrow those callers."""
    ctx = AuthContext(tenant_id="t1")
    ctx.enforce_self_agent("someone-else")  # no raise


def test_enforce_self_agent_allows_naming_yourself():
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    ctx.enforce_self_agent("agent-a")  # no raise


def test_enforce_self_agent_blocks_a_peer():
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_self_agent("agent-b")
    assert exc_info.value.status_code == 403
    # Pinned as a substring by test_route_authz_gaps and
    # test_h06_m30_recall_identity, so it is part of the contract.
    assert "does not match the authenticated agent identity" in exc_info.value.detail
    assert "agent-b" in exc_info.value.detail


def test_enforce_self_agent_allows_an_unasserted_identity():
    """Omission means "use the authenticated identity", or a deliberately wider
    aggregate — never "act as someone else". Every optional agent_id parameter
    defaults to ``None``, so an omitted one arrives here."""
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    ctx.enforce_self_agent(None)  # no raise


def test_enforce_self_agent_refuses_an_explicitly_empty_agent_id():
    """``?agent_id=`` is an assertion, not an omission.

    This is the one input the eight call sites disagreed about: five compared
    unconditionally and answered 403, three guarded on truthiness and treated
    it as omitted. Reconciled to the stricter reading — on ``GET /stm/notes``
    the lenient one would have turned a 403 into a 200 with an empty body.
    """
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_self_agent("")
    assert exc_info.value.status_code == 403


def test_enforce_self_agent_names_the_field_it_was_given():
    """``/recall`` refuses two different knobs and the message has to say which
    — the two used to be separate hand-written raises."""
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_self_agent("agent-b", field="filter_agent_id")
    assert exc_info.value.detail.startswith("filter_agent_id 'agent-b'")


def test_enforce_self_agent_keeps_a_route_specific_detail():
    ctx = AuthContext(tenant_id="t1", agent_id="agent-a")
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_self_agent(
            "agent-b", detail="Agents can only tune their own search profile."
        )
    assert exc_info.value.detail == "Agents can only tune their own search profile."


def test_enforce_self_agent_exempts_admin_without_a_special_case():
    """Admin is exempt because the admin context carries no ``agent_id``, not
    because the gate tests ``is_admin``. Pinned so that if an admin context ever
    grows an agent id, this fails and the exemption gets stated deliberately."""
    ctx = AuthContext(tenant_id=None, is_admin=True)
    assert ctx.agent_id is None
    ctx.enforce_self_agent("any-agent")  # no raise
