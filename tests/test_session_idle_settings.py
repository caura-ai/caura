"""CAURA-652: org-level ``security.session_idle_timeout_minutes``
setting + validator. OSS storage / validator surface only — the
enterprise auth middleware that enforces idle logout has its own
test surface.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestSessionIdleTimeoutSettings:
    def test_default_is_30(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({})
        assert config.session_idle_timeout_minutes == 30

    def test_override_within_range(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({"security": {"session_idle_timeout_minutes": 15}})
        assert config.session_idle_timeout_minutes == 15

    def test_default_settings_has_security_key(self):
        from core_api.services.organization_settings import DEFAULT_SETTINGS

        assert "security" in DEFAULT_SETTINGS
        assert "session_idle_timeout_minutes" in DEFAULT_SETTINGS["security"]

    def test_validator_rejects_out_of_range_low(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match=r"\[5, 120\]"):
            _validate_leaf_types({"security": {"session_idle_timeout_minutes": 4}})

    def test_validator_rejects_out_of_range_high(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match=r"\[5, 120\]"):
            _validate_leaf_types({"security": {"session_idle_timeout_minutes": 121}})

    def test_validator_rejects_non_int(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match="must be int"):
            _validate_leaf_types({"security": {"session_idle_timeout_minutes": "30"}})

    def test_validator_accepts_in_range(self):
        from core_api.services.organization_settings import _validate_leaf_types

        _validate_leaf_types({"security": {"session_idle_timeout_minutes": 5}})
        _validate_leaf_types({"security": {"session_idle_timeout_minutes": 30}})
        _validate_leaf_types({"security": {"session_idle_timeout_minutes": 120}})


@pytest.mark.unit
class TestIntFieldBoolRejection:
    """Python's ``bool`` is a subclass of ``int``; without the
    ``isinstance(v, bool) and bool not in expected_types`` guard,
    ``{"some_int_field": true}`` silently passes type validation and
    then surfaces a confusing range-check error. Cover all three int
    fields the validator currently knows about so a future regression
    on the guard is caught for any of them.
    """

    @pytest.mark.parametrize(
        "payload,match",
        [
            ({"security": {"session_idle_timeout_minutes": True}}, "must be int"),
            ({"lifecycle": {"memory_retention_days": True}}, "must be int"),
            (
                {"security_audit": {"alert_critical_findings_min": False}},
                "must be int",
            ),
        ],
    )
    def test_int_fields_reject_bool(self, payload, match):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match=match):
            _validate_leaf_types(payload)

    def test_bool_fields_still_accept_bool(self):
        """The fix must not regress the bool-typed fields."""
        from core_api.services.organization_settings import _validate_leaf_types

        _validate_leaf_types({"lifecycle": {"lifecycle_automation_enabled": True}})
        _validate_leaf_types({"lifecycle": {"lifecycle_automation_enabled": False}})
        _validate_leaf_types({"crystallizer": {"auto_crystallize": True}})
