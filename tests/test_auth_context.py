"""Unit tests for AuthContext enforcement methods."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext


def test_enforce_read_only_allows_non_demo():
    ctx = AuthContext(tenant_id="t1", is_demo=False)
    ctx.enforce_read_only()  # no raise


def test_enforce_read_only_blocks_demo():
    ctx = AuthContext(tenant_id="t1", is_demo=True)
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_read_only()
    assert exc_info.value.status_code == 403
    assert "demo" in exc_info.value.detail.lower()


def test_enforce_usage_limits_allows_normal_org():
    ctx = AuthContext(tenant_id="t1", is_read_only=False)
    ctx.enforce_usage_limits()  # no raise


def test_enforce_usage_limits_blocks_read_only_org():
    ctx = AuthContext(tenant_id="t1", is_read_only=True)
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_usage_limits()
    assert exc_info.value.status_code == 403
    assert "read-only" in exc_info.value.detail.lower()
    assert "upgrade" in exc_info.value.detail.lower()


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
    assert "read-only" in exc_info.value.detail.lower()


# ── enforce_read_only also enforces scope (composite gate) ───────────


def test_enforce_read_only_blocks_scope_restricted_keys():
    """The write-side aggregate gate at every endpoint head — demo +
    scope are both checked in one call so existing handlers don't need
    per-site changes to honor mcx_ read-only credentials."""
    ctx = AuthContext(
        tenant_id="t1",
        scopes={"recall", "search", "memories_read", "documents_read"},
    )
    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_read_only()
    assert exc_info.value.status_code == 403
    assert "read-only" in exc_info.value.detail.lower()


def test_enforce_read_only_passes_when_write_in_scopes():
    ctx = AuthContext(tenant_id="t1", scopes={"recall", "write"})
    ctx.enforce_read_only()  # no raise


def test_enforce_read_only_passes_when_scopes_unset():
    """Legacy / full-scope keys (scopes=None) must still pass — this
    is the most common path."""
    ctx = AuthContext(tenant_id="t1")
    ctx.enforce_read_only()  # no raise
