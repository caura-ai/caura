"""``AuthContext.enforce_tenant`` — the write-side tenant gate.

Regression cover for a silent bypass: the guard is a bare ``self.tenant_id !=
requested_tenant``, and ``None != None`` is ``False``, so a caller with no
tenant of its own that also named no tenant slipped through with no write
scope at all. That pairing is reachable — the ``CAURA_API_KEY`` path (auth
Path 2) builds ``AuthContext(tenant_id=None)`` for a valid key sent without an
``x-tenant-id`` header, and several write bodies carry ``tenant_id: str | None``
— so a request omitting the tenant reaches ``enforce_tenant(None)`` on a
tenantless context.

The resolution mirrors #987's ``_require_tenant``: naming no tenant is a 400
(a request problem, the credential is authenticated), every other unmatched
case stays a 403.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api import errors
from core_api.auth import AuthContext


def test_tenantless_caller_naming_no_tenant_is_rejected() -> None:
    """The bypass. Without the guard this returns ``None`` and authorizes a
    write with neither a scope nor a target."""
    auth = AuthContext(tenant_id=None, is_admin=False)
    with pytest.raises(HTTPException) as exc:
        auth.enforce_tenant(None)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == errors.AUTH_TENANT_REQUIRED


def test_admin_bypasses_regardless_of_tenant() -> None:
    auth = AuthContext(tenant_id=None, is_admin=True)
    assert auth.enforce_tenant(None) is None
    assert auth.enforce_tenant("any-tenant") is None


def test_matching_tenant_passes() -> None:
    auth = AuthContext(tenant_id="acme", is_admin=False)
    assert auth.enforce_tenant("acme") is None


def test_mismatched_tenant_is_forbidden() -> None:
    auth = AuthContext(tenant_id="acme", is_admin=False)
    with pytest.raises(HTTPException) as exc:
        auth.enforce_tenant("other")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == errors.AUTH_TENANT_MISMATCH


def test_tenant_key_naming_no_tenant_is_bad_request() -> None:
    """A scoped key that names no target is a request problem, not a mismatch —
    400, per #987, rather than the pre-fix 403."""
    auth = AuthContext(tenant_id="acme", is_admin=False)
    with pytest.raises(HTTPException) as exc:
        auth.enforce_tenant(None)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == errors.AUTH_TENANT_REQUIRED


def test_tenantless_caller_naming_a_tenant_is_forbidden() -> None:
    """A tenantless non-admin credential cannot write to a tenant it names —
    it holds no write scope. Stays a 403, not the 400 above."""
    auth = AuthContext(tenant_id=None, is_admin=False)
    with pytest.raises(HTTPException) as exc:
        auth.enforce_tenant("acme")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == errors.AUTH_TENANT_MISMATCH
