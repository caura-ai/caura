"""Admin observation routes used by lifecycle smoke probes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from core_api.auth import AuthContext
from core_api.routes import lifecycle
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


def _admin() -> AuthContext:
    return AuthContext(tenant_id=None, is_admin=True)


async def test_exact_audit_read_is_admin_only() -> None:
    tenant_auth = AuthContext(tenant_id="tenant-a", is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await lifecycle.get_lifecycle_audit(41, org_id="canary", auth=tenant_auth)

    assert exc.value.status_code == 403


async def test_exact_audit_read_returns_storage_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "audit_id": 41,
        "org_id": "canary",
        "action": "archive-expired",
        "status": "success",
    }
    storage = AsyncMock()
    storage.get_lifecycle_audit_row.return_value = row
    monkeypatch.setattr(lifecycle, "get_storage_client", lambda: storage)

    assert (
        await lifecycle.get_lifecycle_audit(41, org_id="canary", auth=_admin()) == row
    )
    storage.get_lifecycle_audit_row.assert_awaited_once_with(41, org_id="canary")


async def test_exact_audit_read_maps_missing_row_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AsyncMock()
    storage.get_lifecycle_audit_row.return_value = None
    monkeypatch.setattr(lifecycle, "get_storage_client", lambda: storage)

    with pytest.raises(HTTPException) as exc:
        await lifecycle.get_lifecycle_audit(404, org_id="canary", auth=_admin())

    assert exc.value.status_code == 404


async def test_summary_is_admin_only() -> None:
    tenant_auth = AuthContext(tenant_id="tenant-a", is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await lifecycle.lifecycle_audit_summary(auth=tenant_auth)

    assert exc.value.status_code == 403


async def test_summary_forwards_bounded_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {
        "since_hours": 30,
        "triggered_by": "core-operations",
        "actions": {},
    }
    storage = AsyncMock()
    storage.get_lifecycle_audit_summary.return_value = expected
    monkeypatch.setattr(lifecycle, "get_storage_client", lambda: storage)

    body = await lifecycle.lifecycle_audit_summary(
        since_hours=30,
        triggered_by="core-operations",
        auth=_admin(),
    )

    assert body == expected
    storage.get_lifecycle_audit_summary.assert_awaited_once_with(
        since_hours=30,
        triggered_by="core-operations",
    )


@pytest.mark.parametrize("since_hours", [0, 169])
async def test_summary_rejects_unbounded_window(since_hours: int) -> None:
    with pytest.raises(HTTPException) as exc:
        await lifecycle.lifecycle_audit_summary(
            since_hours=since_hours,
            auth=_admin(),
        )

    assert exc.value.status_code == 422
