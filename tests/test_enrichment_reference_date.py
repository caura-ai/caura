"""Test that enrich_memory() uses reference_datetime for the enrichment prompt date.

When a caller supplies reference_datetime, the LLM prompt should say
"Today's date is <that date>", not the actual system date.  This is the
key fix for temporal-reasoning accuracy: memories like "Met Sarah last week"
get their dates resolved relative to the session date, not build-time.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestEnrichmentReferenceDate:
    """Verify reference_datetime flows into the enrichment prompt."""

    def test_prompt_uses_reference_datetime(self):
        """When reference_datetime is given, the prompt should contain that date."""
        from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

        ref_dt = datetime(2023, 5, 20, tzinfo=timezone.utc)
        prompt = ENRICHMENT_PROMPT.format(
            content="Met Sarah last week", today=ref_dt.date().isoformat()
        )
        assert "2023-05-20" in prompt
        assert "Today's date is 2023-05-20" in prompt

    def test_prompt_uses_today_when_no_reference(self):
        """When reference_datetime is None, the prompt should contain today's date."""
        from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

        prompt = ENRICHMENT_PROMPT.format(
            content="Some content", today=date.today().isoformat()
        )
        assert date.today().isoformat() in prompt

    @pytest.mark.asyncio
    async def test_enrich_memory_passes_reference_to_prompt(self):
        """enrich_memory() with reference_datetime should format the prompt with that date."""
        from core_api.services.memory_enrichment import enrich_memory

        ref_dt = datetime(2023, 5, 20, tzinfo=timezone.utc)
        captured_prompts = []

        async def fake_complete_json(prompt: str):
            captured_prompts.append(prompt)
            return {
                "memory_type": "episode",
                "weight": 0.7,
                "title": "Met Sarah",
                "summary": "Met Sarah last week",
                "tags": ["sarah", "meeting"],
                "status": "active",
                "ts_valid_start": "2023-05-13T00:00:00+00:00",
                "ts_valid_end": None,
                "contains_pii": False,
                "pii_types": [],
            }

        mock_llm = AsyncMock()
        mock_llm.complete_json = fake_complete_json

        # Build a tenant_config that enables enrichment and uses a non-fake provider
        tenant_config = MagicMock()
        tenant_config.enrichment_provider = "openai"
        tenant_config.enrichment_model = None

        async def mock_fallback(*args, **kwargs):
            call_fn = args[1] if len(args) > 1 else kwargs.get("call_fn")
            return await call_fn(mock_llm)

        with patch(
            "common.enrichment.service.call_with_fallback", side_effect=mock_fallback
        ):
            result = await enrich_memory(
                "Met Sarah last week", tenant_config, reference_datetime=ref_dt
            )

        assert len(captured_prompts) == 1
        assert "2023-05-20" in captured_prompts[0]
        assert "Today's date is 2023-05-20" in captured_prompts[0]
        assert result.memory_type == "episode"

    @pytest.mark.asyncio
    async def test_enrich_memory_uses_today_without_reference(self):
        """enrich_memory() without reference_datetime should use today's date."""
        from core_api.services.memory_enrichment import enrich_memory

        captured_prompts = []

        async def fake_complete_json(prompt: str):
            captured_prompts.append(prompt)
            return {
                "memory_type": "fact",
                "weight": 0.7,
                "title": "Test",
                "summary": "Test",
                "tags": [],
                "status": "active",
                "ts_valid_start": None,
                "ts_valid_end": None,
                "contains_pii": False,
                "pii_types": [],
            }

        mock_llm = AsyncMock()
        mock_llm.complete_json = fake_complete_json

        tenant_config = MagicMock()
        tenant_config.enrichment_provider = "openai"
        tenant_config.enrichment_model = None

        async def mock_fallback(*args, **kwargs):
            call_fn = args[1] if len(args) > 1 else kwargs.get("call_fn")
            return await call_fn(mock_llm)

        with patch(
            "common.enrichment.service.call_with_fallback", side_effect=mock_fallback
        ):
            await enrich_memory("Some content", tenant_config)

        assert len(captured_prompts) == 1
        assert f"Today's date is {date.today().isoformat()}" in captured_prompts[0]
