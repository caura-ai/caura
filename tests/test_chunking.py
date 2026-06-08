"""Auto-chunking on direct write tests.

Unit tests validate:
- CHUNKING_THRESHOLD_CHARS constant value is 2000
- Auto-chunking is disabled by default in ResolvedConfig
- Auto-chunking can be enabled via tenant settings
"""

import pytest

from core_api.constants import CHUNKING_THRESHOLD_CHARS, MAX_CONTENT_LENGTH
from core_api.services.organization_settings import DEFAULT_SETTINGS, ResolvedConfig


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkingConstants:
    def test_chunking_threshold_value(self):
        assert CHUNKING_THRESHOLD_CHARS == 2000

    def test_chunking_threshold_less_than_max_content(self):
        assert CHUNKING_THRESHOLD_CHARS < MAX_CONTENT_LENGTH


@pytest.mark.unit
class TestChunkingTenantSettings:
    def test_default_settings_has_chunking_section(self):
        assert "chunking" in DEFAULT_SETTINGS
        assert "auto_chunk_enabled" in DEFAULT_SETTINGS["chunking"]
        assert DEFAULT_SETTINGS["chunking"]["auto_chunk_enabled"] is None

    def test_auto_chunk_disabled_by_default(self):
        config = ResolvedConfig({})
        assert config.auto_chunk_enabled is False

    def test_auto_chunk_enabled_via_settings(self):
        config = ResolvedConfig({"chunking": {"auto_chunk_enabled": True}})
        assert config.auto_chunk_enabled is True

    def test_auto_chunk_explicitly_disabled(self):
        config = ResolvedConfig({"chunking": {"auto_chunk_enabled": False}})
        assert config.auto_chunk_enabled is False
