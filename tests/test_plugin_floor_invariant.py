"""Guard the plugin-version floors across a floor reconcile.

The auto-upgrade TARGET (``MIN_RECOMMENDED_PLUGIN_VERSION``) must stay at or
above the auto-deploy ELIGIBILITY floor (``MIN_AUTO_DEPLOY_PLUGIN_VERSION``);
otherwise the heartbeat could queue an auto-deploy to a version below the
safe-to-self-deploy line. These run without a DB.
"""

from __future__ import annotations

from core_api.version_compat import (
    MIN_AUTO_DEPLOY_PLUGIN_VERSION,
    MIN_RECOMMENDED_PLUGIN_VERSION,
    _parse,
    is_plugin_outdated,
)


def test_recommended_at_or_above_auto_deploy_floor():
    assert _parse(MIN_RECOMMENDED_PLUGIN_VERSION) >= _parse(
        MIN_AUTO_DEPLOY_PLUGIN_VERSION
    ), (
        f"recommended floor {MIN_RECOMMENDED_PLUGIN_VERSION} must be >= "
        f"auto-deploy floor {MIN_AUTO_DEPLOY_PLUGIN_VERSION}"
    )


def test_recommended_floor_parses_to_nonempty():
    assert _parse(MIN_RECOMMENDED_PLUGIN_VERSION), (
        "recommended floor must be a parseable version"
    )


def test_outdated_semantics_around_floor():
    # Anything clearly below the floor is outdated; the floor itself and
    # anything above are current. (Floor-value-agnostic so it survives bumps.)
    assert is_plugin_outdated("0.0.1") is True
    assert is_plugin_outdated(MIN_RECOMMENDED_PLUGIN_VERSION) is False
    assert is_plugin_outdated("999.0.0") is False
    assert is_plugin_outdated(None) is False
