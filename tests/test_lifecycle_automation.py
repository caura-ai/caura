"""Lifecycle automation — settings + the surviving crystallize+entity-link
service entry point.

Pre-CAURA-655 this file also asserted the in-process scheduler interval
constant; the scheduler moved to core-operations and the constant is
no longer load-bearing on core-api. Cron cadence now lives on
``core_operations.config.Settings.lifecycle_archive_interval_seconds``.
"""

import pytest

from core_api.constants import (
    LIFECYCLE_BATCH_SIZE,
    LIFECYCLE_STALE_ARCHIVE_WEIGHT,
)


@pytest.mark.unit
class TestLifecycleConstants:
    def test_batch_size(self):
        assert LIFECYCLE_BATCH_SIZE == 500

    def test_stale_archive_weight(self):
        assert LIFECYCLE_STALE_ARCHIVE_WEIGHT == 0.3

    def test_batch_size_reasonable(self):
        assert 50 <= LIFECYCLE_BATCH_SIZE <= 5000


@pytest.mark.unit
class TestLifecycleTenantSettings:
    def test_enabled_by_default(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({})
        assert config.lifecycle_automation_enabled is True

    def test_can_be_disabled(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({"lifecycle": {"lifecycle_automation_enabled": False}})
        assert config.lifecycle_automation_enabled is False

    def test_default_settings_has_lifecycle_section(self):
        from core_api.services.organization_settings import DEFAULT_SETTINGS

        assert "lifecycle" in DEFAULT_SETTINGS
        assert "lifecycle_automation_enabled" in DEFAULT_SETTINGS["lifecycle"]


@pytest.mark.unit
class TestLifecycleServiceImports:
    def test_module_importable(self):
        from core_api.services import lifecycle_service  # noqa: F401

    def test_has_run_for_tenant(self):
        from core_api.services.lifecycle_service import run_lifecycle_for_tenant

        assert callable(run_lifecycle_for_tenant)

    def test_scheduler_is_gone(self):
        # Guard against the loop sneaking back in. The
        # core-operations service owns the cron now; resurrecting the
        # in-process loop here would re-create the double-scheduling
        # situation CAURA-655 set out to remove.
        from core_api.services import lifecycle_service

        assert not hasattr(lifecycle_service, "lifecycle_scheduler")


@pytest.mark.unit
class TestLifecycleTopics:
    def test_topic_strings(self):
        from common.events.topics import Topics

        assert (
            Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED
            == "memclaw.lifecycle.archive-expired-requested"
        )
        assert (
            Topics.Lifecycle.ARCHIVE_STALE_REQUESTED
            == "memclaw.lifecycle.archive-stale-requested"
        )
        assert (
            Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED
            == "memclaw.lifecycle.purge-soft-deleted-requested"
        )

    def test_topic_strenum_format(self):
        from common.events.topics import Topics

        # ``StrEnum`` so f-strings see the literal value, not the
        # ``Lifecycle.ARCHIVE_EXPIRED_REQUESTED`` repr — same invariant
        # the embed/enrich topics rely on for Pub/Sub's ``topic_path``.
        assert (
            f"{Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED}"
            == "memclaw.lifecycle.archive-expired-requested"
        )


@pytest.mark.unit
class TestMemoryRetentionSettings:
    """CAURA-656: org-level memory_retention_days setting + validator."""

    def test_default_is_30(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({})
        assert config.memory_retention_days == 30

    def test_override_within_range(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({"lifecycle": {"memory_retention_days": 7}})
        assert config.memory_retention_days == 7

    def test_default_settings_has_retention_key(self):
        from core_api.services.organization_settings import DEFAULT_SETTINGS

        assert "memory_retention_days" in DEFAULT_SETTINGS["lifecycle"]

    def test_validator_rejects_out_of_range_low(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match=r"\[1, 30\]"):
            _validate_leaf_types({"lifecycle": {"memory_retention_days": 0}})

    def test_validator_rejects_out_of_range_high(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match=r"\[1, 30\]"):
            _validate_leaf_types({"lifecycle": {"memory_retention_days": 31}})

    def test_validator_rejects_non_int(self):
        from core_api.services.organization_settings import _validate_leaf_types

        with pytest.raises(ValueError, match="must be int"):
            _validate_leaf_types({"lifecycle": {"memory_retention_days": "30"}})

    def test_validator_accepts_in_range(self):
        from core_api.services.organization_settings import _validate_leaf_types

        # Should not raise.
        _validate_leaf_types({"lifecycle": {"memory_retention_days": 1}})
        _validate_leaf_types({"lifecycle": {"memory_retention_days": 30}})
        _validate_leaf_types({"lifecycle": {"memory_retention_days": 15}})
