"""ENT2-095, ENT2-168: a write refused for a read-only org says why, and the way back.

Read-only mode compares the month's operation counters, which deleting data does
not lower, so the refusal no longer offers deleting as the way back. The platform
names the cause in ``X-Org-Read-Only-Reason``: an on-prem licence that is missing
or expired is not a plan limit, and REST now says so, as MCP already did.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core_api.auth import get_auth_context
from core_api.config import settings
from tests._legacy_contracts import LEGACY_API_KEY_FIELD

pytestmark = pytest.mark.unit


@pytest.fixture
def _gateway_path(monkeypatch):
    """Path 4 (X-Tenant-ID), the one the gateway takes."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "admin_api_key", "")
    monkeypatch.setattr(settings, "api_key", "")


async def _refusal(reason: str | None) -> dict:
    headers = {"x-tenant-id": "t1", "x-org-read-only": "true"}
    if reason is not None:
        headers["x-org-read-only-reason"] = reason
    ctx = await get_auth_context(SimpleNamespace(headers=headers), key=None)
    with pytest.raises(HTTPException) as refused:
        ctx.enforce_usage_limits()
    return refused.value.detail


@pytest.mark.parametrize("reason", [None, "organization", "spoofed"])
async def test_a_plan_limit_refusal_never_offers_deleting_data(_gateway_path, reason):
    detail = await _refusal(reason)
    assert detail["code"] == "PLAN_LIMIT_READ_ONLY"
    assert "delete data" not in detail["message"]
    assert "monthly" in detail["message"]
    assert "does not lower" in detail["details"]["remediation"]


async def test_an_expired_licence_is_named_not_called_a_plan_limit(_gateway_path):
    detail = await _refusal("license")
    assert detail["code"] == "PLAN_LIMIT_READ_ONLY"
    assert "license" in detail["message"]
    assert "plan limits" not in detail["message"]
    assert detail["details"]["read_only_reason"] == "license"


async def test_a_reason_without_the_read_only_verdict_refuses_nothing(_gateway_path):
    headers = {"x-tenant-id": "t1", "x-org-read-only-reason": "license"}
    ctx = await get_auth_context(SimpleNamespace(headers=headers), key=None)
    ctx.enforce_usage_limits()
