"""Fix #10: Lifecycle automation — scheduled archival of expired/stale memories.

Unit tests validate:
- Lifecycle constants
- Tenant setting defaults and overrides
- Archive logic contracts
"""

import pytest

from core_api.constants import (
    LIFECYCLE_BATCH_SIZE,
    LIFECYCLE_INTERVAL_HOURS,
    LIFECYCLE_STALE_ARCHIVE_WEIGHT,
)


@pytest.mark.unit
class TestLifecycleConstants:
    """Verify lifecycle constant values."""

    def test_interval_hours(self):
        assert LIFECYCLE_INTERVAL_HOURS == 24

    def test_batch_size(self):
        assert LIFECYCLE_BATCH_SIZE == 500

    def test_stale_archive_weight(self):
        assert LIFECYCLE_STALE_ARCHIVE_WEIGHT == 0.3

    def test_interval_reasonable(self):
        assert 1 <= LIFECYCLE_INTERVAL_HOURS <= 168  # 1h to 1 week

    def test_batch_size_reasonable(self):
        assert 50 <= LIFECYCLE_BATCH_SIZE <= 5000


@pytest.mark.unit
class TestLifecycleTenantSettings:
    """Verify lifecycle tenant setting integration."""

    def test_enabled_by_default(self):
        from core_api.services.organization_settings import ResolvedConfig
        config = ResolvedConfig({})
        assert config.lifecycle_automation_enabled is True

    def test_can_be_disabled(self):
        from core_api.services.organization_settings import ResolvedConfig
        config = ResolvedConfig({"lifecycle": {"lifecycle_automation_enabled": False}})
        assert config.lifecycle_automation_enabled is False

    def test_explicitly_enabled(self):
        from core_api.services.organization_settings import ResolvedConfig
        config = ResolvedConfig({"lifecycle": {"lifecycle_automation_enabled": True}})
        assert config.lifecycle_automation_enabled is True

    def test_default_settings_has_lifecycle_section(self):
        from core_api.services.organization_settings import DEFAULT_SETTINGS
        assert "lifecycle" in DEFAULT_SETTINGS
        assert "lifecycle_automation_enabled" in DEFAULT_SETTINGS["lifecycle"]


@pytest.mark.unit
class TestLifecycleServiceImports:
    """Verify lifecycle service module is importable and has expected functions."""

    def test_module_importable(self):
        from core_api.services import lifecycle_service  # noqa: F401

    def test_has_scheduler(self):
        from core_api.services.lifecycle_service import lifecycle_scheduler
        assert callable(lifecycle_scheduler)

    def test_has_run_for_tenant(self):
        from core_api.services.lifecycle_service import run_lifecycle_for_tenant
        assert callable(run_lifecycle_for_tenant)
