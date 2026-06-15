"""Unit tests for the governance config block (DEFAULT_SETTINGS + validators +
ResolvedConfig accessors). Pure logic — no DB."""

import pytest

from common.governance import PIICategory
from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    ResolvedConfig,
    _check_keys,
    _validate_governance_enums,
    _validate_leaf_types,
)


def test_default_governance_is_opt_in_and_safe():
    rc = ResolvedConfig({})  # no overrides → safe defaults
    assert rc.governance_pii.enabled is False
    assert rc.governance_pii.action == "flag"  # non-destructive
    assert rc.governance_pii.enabled_categories is None
    assert rc.governance_non_business.enabled is False
    assert rc.governance_non_business.disposition == "store"  # no filtering


def test_resolved_pii_categories_from_overrides():
    rc = ResolvedConfig(
        {
            "governance": {
                "pii": {
                    "enabled": True,
                    "action": "mask",
                    "categories": {"email": True, "credit_card": True, "phone": False},
                }
            }
        }
    )
    pii = rc.governance_pii
    assert pii.enabled and pii.action == "mask"
    assert pii.enabled_categories == frozenset(
        {PIICategory.EMAIL, PIICategory.CREDIT_CARD}
    )


def test_enabled_with_no_categories_scans_all():
    rc = ResolvedConfig({"governance": {"pii": {"enabled": True, "action": "drop"}}})
    assert (
        rc.governance_pii.enabled_categories is None
    )  # None → scan all (secure default)


def test_non_business_disposition_override():
    rc = ResolvedConfig(
        {
            "governance": {
                "non_business": {"enabled": True, "disposition": "keep_private"}
            }
        }
    )
    nb = rc.governance_non_business
    assert nb.enabled and nb.disposition == "keep_private"


def test_validate_rejects_bad_action():
    with pytest.raises(ValueError, match="governance.pii.action"):
        _validate_governance_enums({"governance": {"pii": {"action": "nuke"}}})


def test_validate_rejects_bad_disposition():
    with pytest.raises(ValueError, match="governance.non_business.disposition"):
        _validate_governance_enums(
            {"governance": {"non_business": {"disposition": "vaporize"}}}
        )


def test_validate_accepts_good_enums_and_absent_block():
    # Valid values + a payload with no governance block must both pass.
    _validate_governance_enums(
        {
            "governance": {
                "pii": {"action": "mask"},
                "non_business": {"disposition": "drop"},
            }
        }
    )
    _validate_governance_enums({"enrichment": {"enabled": True}})


def test_check_keys_rejects_unknown_governance_key():
    with pytest.raises(ValueError, match="Unknown settings key"):
        _check_keys({"governance": {"pii": {"bogus": True}}}, DEFAULT_SETTINGS)


def test_leaf_types_reject_non_bool_category():
    with pytest.raises(ValueError, match="governance.pii.categories.email"):
        _validate_leaf_types({"governance": {"pii": {"categories": {"email": "yes"}}}})


def test_default_settings_has_all_seven_categories():
    cats = DEFAULT_SETTINGS["governance"]["pii"]["categories"]
    assert set(cats) == {c.value for c in PIICategory}
    assert all(v is False for v in cats.values())  # opt-in
