"""M-116: security audits are configured per org, and nowhere else.

core-api declared seven ``SECURITY_AUDIT_*`` settings as fleet-wide defaults,
and seven ``ResolvedConfig`` properties fell back to them. Nothing called those
properties. The scheduler and the threshold alerts run in enterprise
platform-admin-api, which reads an org's ``security_audit`` block from
``GET /api/v1/settings``: the org's stored overrides merged over
``DEFAULT_SETTINGS``, with no env value applied. An operator who set one got no
error and no effect, while a malformed ``SECURITY_AUDIT_SCHEDULE_CRON`` still
stopped core-api starting, over a value nothing used.
"""

from collections.abc import Iterable

import pytest

from core_api.config import Settings
from core_api.services.organization_settings import ResolvedConfig


def _security_audit_names(names: Iterable[str]) -> list[str]:
    return sorted(name for name in names if name.startswith("security_audit"))


def test_core_api_declares_no_security_audit_settings() -> None:
    """Bringing one back means wiring it into the view enterprise reads."""
    assert _security_audit_names(Settings.model_fields) == []
    assert _security_audit_names(vars(ResolvedConfig)) == []


def test_a_leftover_security_audit_variable_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that exported one before the upgrade still boots."""
    monkeypatch.setenv("SECURITY_AUDIT_SCHEDULE_CRON", "not a cron expression")
    monkeypatch.setenv("SECURITY_AUDIT_ALERT_RECIPIENTS", "ops@example.com")

    Settings(_env_file=None)
