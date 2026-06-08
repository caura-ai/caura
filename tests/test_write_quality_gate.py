"""Write-time quality gate: reject memories with content shorter than CRYSTALLIZER_SHORT_CONTENT_CHARS.

Unit tests — no database required.
"""

import pytest
from unittest.mock import AsyncMock

from core_api.constants import CRYSTALLIZER_SHORT_CONTENT_CHARS


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQualityGateConstants:
    def test_min_content_chars_is_10(self):
        assert CRYSTALLIZER_SHORT_CONTENT_CHARS == 10

    def test_min_content_chars_positive(self):
        assert CRYSTALLIZER_SHORT_CONTENT_CHARS > 0


# ---------------------------------------------------------------------------
# create_memory rejection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateMemoryQualityGate:
    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def make_data(self):
        """Factory for MemoryCreate-like objects."""
        from core_api.schemas import MemoryCreate

        def _make(content: str, **kwargs):
            defaults = {
                "tenant_id": "test-tenant",
                "agent_id": "test-agent",
                "content": content,
            }
            defaults.update(kwargs)
            return MemoryCreate(**defaults)

        return _make

    async def test_rejects_empty_content(self, mock_db, make_data):
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory

        with pytest.raises(HTTPException) as exc_info:
            await create_memory(mock_db, make_data(" "))
        assert exc_info.value.status_code == 422
        assert "too short" in exc_info.value.detail

    async def test_rejects_short_content(self, mock_db, make_data):
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory

        with pytest.raises(HTTPException) as exc_info:
            await create_memory(mock_db, make_data("ok"))
        assert exc_info.value.status_code == 422

    async def test_rejects_whitespace_padded_short(self, mock_db, make_data):
        """'  hi  ' strips to 2 chars — should reject."""
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory

        with pytest.raises(HTTPException) as exc_info:
            await create_memory(mock_db, make_data("  hi  "))
        assert exc_info.value.status_code == 422

    async def test_rejects_exactly_9_chars(self, mock_db, make_data):
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory

        with pytest.raises(HTTPException) as exc_info:
            await create_memory(mock_db, make_data("123456789"))
        assert exc_info.value.status_code == 422

    async def test_accepts_exactly_10_chars(self, mock_db, make_data):
        """10 chars should pass the quality gate (not raise 422 'too short')."""
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory

        # Should NOT raise a 422 "too short" — may succeed or raise other errors
        try:
            await create_memory(mock_db, make_data("1234567890"))
        except HTTPException as exc:
            assert exc.status_code != 422 or "too short" not in str(exc.detail)
        except Exception:
            pass  # Any non-422 error is fine — gate wasn't triggered


# Bulk short-content rejection moved to per-item errors under CAURA-599;
# end-to-end coverage lives in tests/test_bulk_atomicity.py. Single-write
# gate (TestCreateMemoryQualityGate above) still raises 422 via the
# pipeline's check_content_length step — unchanged.
