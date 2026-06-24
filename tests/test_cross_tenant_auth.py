"""Unit tests for cross-tenant read plumbing in get_auth_context.

The gateway injects ``X-Readable-Tenant-IDs`` (CSV) and ``X-Key-Scopes``
(CSV) when a credential is authorized to read beyond its home tenant.
These tests exercise the Path-4 (X-Tenant-ID) branch in
``get_auth_context`` and confirm that the readable-tenant set and scope
set are parsed and surfaced on ``AuthContext``, while single-tenant
keys retain their original semantics.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings
from core_api.tenant_context import get_readable_tenants


@pytest.fixture
def _disable_standalone(monkeypatch):
    """Match the install-credential test fixture: turn off standalone
    + key-gate paths so Path 4 (X-Tenant-ID header) executes."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, "memclaw_api_key", "")
    monkeypatch.setattr(settings, "admin_api_key", "")
    monkeypatch.setattr(settings, "api_key", "")


def _request(headers: dict[str, str]):
    """Minimal stand-in for ``starlette.requests.Request`` — auth
    only reads ``request.headers.get(...)``."""
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()})


# ── Backward compatibility: single-tenant keys ───────────────────────


@pytest.mark.unit
async def test_single_tenant_key_readable_defaults_to_home(_disable_standalone):
    """Absent X-Readable-Tenant-IDs leaves the caller pinned to the
    home tenant — matches the pre-feature behaviour."""
    request = _request({"X-Tenant-ID": "home-tenant"})
    ctx: AuthContext = await get_auth_context(request, key=None)

    assert ctx.tenant_id == "home-tenant"
    assert ctx.readable_tenant_ids == ["home-tenant"]
    assert ctx.is_cross_tenant_read is False
    assert ctx.scopes is None
    # Context var defaults to empty (single-tenant); the DB session
    # plumbs it as an empty CSV.
    assert get_readable_tenants() == []


# ── Cross-tenant key: readable set parsing ───────────────────────────


@pytest.mark.unit
async def test_cross_tenant_readable_set_parsed(_disable_standalone):
    request = _request(
        {
            "X-Tenant-ID": "tenant-admin",
            "X-Readable-Tenant-IDs": "tenant-a,tenant-b,tenant-c",
        }
    )
    ctx: AuthContext = await get_auth_context(request, key=None)

    assert ctx.tenant_id == "tenant-admin"
    assert ctx.readable_tenant_ids == [
        "tenant-admin",
        "tenant-a",
        "tenant-b",
        "tenant-c",
    ]
    assert ctx.is_cross_tenant_read is True
    # The DB-session context var carries the union with the home tenant
    # prepended so writes still target ``tenant-admin``.
    assert get_readable_tenants() == [
        "tenant-admin",
        "tenant-a",
        "tenant-b",
        "tenant-c",
    ]


@pytest.mark.unit
async def test_cross_tenant_readable_set_strips_whitespace(_disable_standalone):
    request = _request(
        {
            "X-Tenant-ID": "home",
            "X-Readable-Tenant-IDs": " tenant-a , tenant-b ",
        }
    )
    ctx = await get_auth_context(request, key=None)

    assert ctx.readable_tenant_ids == ["home", "tenant-a", "tenant-b"]


@pytest.mark.unit
async def test_cross_tenant_readable_set_drops_empty_entries(_disable_standalone):
    request = _request(
        {
            "X-Tenant-ID": "home",
            "X-Readable-Tenant-IDs": ",,tenant-a,,",
        }
    )
    ctx = await get_auth_context(request, key=None)

    assert ctx.readable_tenant_ids == ["home", "tenant-a"]


# ── Scopes parsing + write-gate ──────────────────────────────────────


@pytest.mark.unit
async def test_scopes_parsed_from_header(_disable_standalone):
    request = _request(
        {
            "X-Tenant-ID": "home",
            "X-Key-Scopes": "recall,search,memories_read,documents_read",
        }
    )
    ctx = await get_auth_context(request, key=None)

    assert ctx.scopes == {"recall", "search", "memories_read", "documents_read"}


@pytest.mark.unit
async def test_no_scope_header_means_full_scope(_disable_standalone):
    """Absent X-Key-Scopes leaves ``scopes=None`` so enforce_write_scope
    is a no-op — single-tenant keys keep their pre-feature behaviour."""
    request = _request({"X-Tenant-ID": "home"})
    ctx = await get_auth_context(request, key=None)

    assert ctx.scopes is None
    ctx.enforce_write_scope()  # no raise


@pytest.mark.unit
async def test_read_only_scopes_block_writes(_disable_standalone):
    """A credential whose scope set lacks ``write`` is rejected by
    enforce_write_scope before any mutating handler runs."""
    request = _request(
        {
            "X-Tenant-ID": "home",
            "X-Key-Scopes": "recall,search,memories_read",
        }
    )
    ctx = await get_auth_context(request, key=None)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        ctx.enforce_write_scope()
    assert exc_info.value.status_code == 403


# ── Header case-insensitivity ────────────────────────────────────────


@pytest.mark.unit
async def test_headers_case_insensitive(_disable_standalone):
    request = _request(
        {
            "X-TENANT-ID": "home",
            "x-readable-tenant-ids": "tenant-a",
            "X-Key-Scopes": "recall",
        }
    )
    ctx = await get_auth_context(request, key=None)

    assert ctx.readable_tenant_ids == ["home", "tenant-a"]
    assert ctx.scopes == {"recall"}


# ── source_tenants_for_audit (audit hook seam) ──────────────────────


@pytest.mark.unit
def test_source_tenants_for_audit_empty_for_single_tenant():
    ctx = AuthContext(tenant_id="home")
    assert ctx.source_tenants_for_audit() == []


@pytest.mark.unit
def test_source_tenants_for_audit_excludes_home():
    """The audit hook returns *source* tenants — never the home, since
    a request always implicitly reads from its home tenant and
    self-attribution would be noise in every source tenant's log."""
    ctx = AuthContext(
        tenant_id="home",
        readable_tenant_ids=["home", "src-a", "src-b"],
    )
    assert ctx.source_tenants_for_audit() == ["src-a", "src-b"]


@pytest.mark.unit
def test_source_tenants_for_audit_empty_for_admin_tenant_none():
    """Admin path: tenant_id=None means no tenant scoping at all.
    Audit hook returns empty so admin reads don't emit per-tenant
    events (admin actions get their own audit category)."""
    ctx = AuthContext(tenant_id=None, is_admin=True)
    assert ctx.source_tenants_for_audit() == []


@pytest.mark.unit
async def test_log_cross_tenant_read_noop_for_single_tenant():
    """Audit emission helper: zero events when source_tenants is empty
    (single-tenant credentials, or cross-tenant credentials that ended
    up only touching home). Hot path — must not fall through to the
    queue/sync POST in that case."""
    from unittest.mock import AsyncMock, patch

    from core_api.services.audit_service import log_cross_tenant_read

    with patch("core_api.services.audit_service.log_action", new=AsyncMock()) as mock:
        await log_cross_tenant_read(            home_tenant_id="home",
            home_agent_id="agent-1",
            source_tenants=[],
            surface="memclaw_recall",
        )
        mock.assert_not_called()


@pytest.mark.unit
async def test_log_cross_tenant_read_emits_per_source_tenant():
    """One event per source tenant. Each event is logged TO the source
    tenant (so per-tenant audit-log queries surface "who read FROM
    me") with home_tenant_id + home_agent_id in detail for forensic
    traceability."""
    from unittest.mock import AsyncMock, patch

    from core_api.services.audit_service import log_cross_tenant_read

    with patch("core_api.services.audit_service.log_action", new=AsyncMock()) as mock:
        await log_cross_tenant_read(            home_tenant_id="home",
            home_agent_id="agent-1",
            source_tenants=["src-a", "src-b"],
            surface="memclaw_recall",
            result_count_by_tenant={"src-a": 3, "src-b": 0},
            query_summary="how do we handle X",
        )
        assert mock.await_count == 2
        first_call = mock.await_args_list[0].kwargs
        assert first_call["tenant_id"] == "src-a"
        assert first_call["action"] == "cross_tenant_read"
        assert first_call["resource_type"] == "memclaw_recall"
        assert first_call["detail"]["home_tenant_id"] == "home"
        assert first_call["detail"]["home_agent_id"] == "agent-1"
        assert first_call["detail"]["result_count_from_this_tenant"] == 3
        assert first_call["detail"]["query_summary"] == "how do we handle X"
        second_call = mock.await_args_list[1].kwargs
        assert second_call["tenant_id"] == "src-b"
        assert second_call["detail"]["result_count_from_this_tenant"] == 0
