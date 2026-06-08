"""Bool-rejection guard on int-typed settings fields.

Python's ``bool`` is a subclass of ``int``, so without an explicit
exclusion ``{"memory_retention_days": true}`` would silently pass the
type check on int-typed fields and then fail the range check with a
confusing "must be in [1, 30], got True" message. The validator's
``wrong_bool = isinstance(v, bool) and bool not in expected_types``
guard catches this for every int-typed setting at once.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestIntFieldBoolRejection:
    """Cover all int fields the validator currently knows about so a
    future regression on the bool/int guard is caught for any of
    them."""

    @pytest.mark.parametrize(
        "payload,match",
        [
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
