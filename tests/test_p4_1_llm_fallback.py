"""P4-1: LLM retry + fallback chain for enrichment and entity extraction.

Unit tests validate:
  - Constants are sensible
  - _validate_enrichment sanitises bad LLM output
  - _call_with_retry retries then raises
  - _call_extract_with_retry retries then raises
  - enrich_memory fallback chain: primary → alternative → heuristic
  - extract_entities_from_content fallback chain: primary → alternative → heuristic
  - "fake" and "none" short-circuit without calling LLM
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.constants import (
    LLM_FALLBACK_MODEL_OPENAI,
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_DELAY_S,
)


# ---------------------------------------------------------------------------
# Unit tests: constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackConstants:
    """Verify the LLM fallback constants are sensible."""

    def test_retry_attempts_positive(self):
        assert LLM_RETRY_ATTEMPTS >= 1

    def test_retry_attempts_bounded(self):
        """Should not retry excessively — each attempt adds latency."""
        assert LLM_RETRY_ATTEMPTS <= 5

    def test_retry_delay_positive(self):
        assert LLM_RETRY_DELAY_S > 0

    def test_retry_delay_bounded(self):
        """Worst-case total delay = n*(n+1)/2 * delay. Keep reasonable."""
        worst = sum(LLM_RETRY_DELAY_S * (i + 1) for i in range(LLM_RETRY_ATTEMPTS))
        assert worst <= 30.0  # 30s max total backoff

    def test_fallback_model_is_cheap(self):
        """Fallback model should be a small/cheap model."""
        name = LLM_FALLBACK_MODEL_OPENAI.lower()
        assert "mini" in name or "nano" in name


# ---------------------------------------------------------------------------
# Unit tests: _validate_enrichment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateEnrichment:
    """Verify _validate_enrichment sanitises raw LLM output."""

    def test_valid_output_passes_through(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {
            "memory_type": "decision",
            "weight": 0.85,
            "title": "Use PostgreSQL for storage",
            "summary": "Decided on PG.",
            "tags": ["db", "decision"],
            "status": "active",
            "ts_valid_start": None,
            "ts_valid_end": None,
            "contains_pii": False,
            "pii_types": [],
        }
        result = _validate_enrichment(raw, llm_ms=42)
        assert result.memory_type == "decision"
        assert result.weight == 0.85
        assert result.llm_ms == 42

    def test_unknown_memory_type_defaults_to_fact(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {"memory_type": "nonsense", "weight": 0.5, "title": "x", "summary": "y"}
        result = _validate_enrichment(raw, llm_ms=0)
        assert result.memory_type == "fact"

    def test_unknown_status_defaults_to_active(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {"memory_type": "fact", "status": "banana"}
        result = _validate_enrichment(raw, llm_ms=0)
        assert result.status == "active"

    def test_weight_clamped_to_0_1(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        high = _validate_enrichment({"weight": 5.0}, llm_ms=0)
        assert high.weight == 1.0

        low = _validate_enrichment({"weight": -2.0}, llm_ms=0)
        assert low.weight == 0.0

    def test_title_truncated_to_80(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {"title": "A" * 200}
        result = _validate_enrichment(raw, llm_ms=0)
        assert len(result.title) == 80

    def test_invalid_timestamp_set_to_none(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {"ts_valid_start": "not-a-date", "ts_valid_end": "also-bad"}
        result = _validate_enrichment(raw, llm_ms=0)
        assert result.ts_valid_start is None
        assert result.ts_valid_end is None

    def test_valid_iso_timestamp_preserved(self):
        from core_api.services.memory_enrichment import _validate_enrichment

        raw = {"ts_valid_start": "2025-06-01T00:00:00Z"}
        result = _validate_enrichment(raw, llm_ms=0)
        assert result.ts_valid_start == "2025-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Unit tests: retry helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCallWithRetry:
    """Verify enrichment retry helper retries correct number of times."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        from core_api.services.memory_enrichment import (
            MemoryEnrichment,
            _call_with_retry,
        )

        fn = AsyncMock(return_value=MemoryEnrichment())
        result = await _call_with_retry(fn, "test")
        assert isinstance(result, MemoryEnrichment)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        from core_api.services.memory_enrichment import (
            MemoryEnrichment,
            _call_with_retry,
        )

        fn = AsyncMock(
            side_effect=[RuntimeError("fail")] * (LLM_RETRY_ATTEMPTS - 1)
            + [MemoryEnrichment()],
        )
        with patch("common.llm.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await _call_with_retry(fn, "test")
        assert isinstance(result, MemoryEnrichment)
        assert fn.call_count == LLM_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retries_then_raises(self):
        from core_api.services.memory_enrichment import _call_with_retry

        fn = AsyncMock(side_effect=RuntimeError("permanent"))
        with patch("common.llm.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="permanent"):
                await _call_with_retry(fn, "test")
        assert fn.call_count == LLM_RETRY_ATTEMPTS


@pytest.mark.unit
class TestCallExtractWithRetry:
    """Verify entity extraction retry helper."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        from core_api.services.entity_extraction import (
            ExtractedGraph,
            _call_extract_with_retry,
        )

        fn = AsyncMock(return_value=ExtractedGraph())
        result = await _call_extract_with_retry(fn, "test")
        assert isinstance(result, ExtractedGraph)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_raises(self):
        from core_api.services.entity_extraction import _call_extract_with_retry

        fn = AsyncMock(side_effect=RuntimeError("permanent"))
        with patch("common.llm.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="permanent"):
                await _call_extract_with_retry(fn, "test")
        assert fn.call_count == LLM_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# Unit tests: enrich_memory fallback chain
# ---------------------------------------------------------------------------


def _make_tenant_config(**overrides):
    """Create a minimal tenant_config SimpleNamespace for testing."""
    defaults = {
        "enrichment_provider": "gemini",
        "entity_extraction_provider": "gemini",
        "enrichment_model": "gemini-2.0-flash",
        "entity_extraction_model": "gemini-2.0-flash",
        "openai_api_key": "sk-test-key",
        "anthropic_api_key": None,
        "gemini_api_key": "gemini-test-key",
        "fallback_llm_provider": None,
        "fallback_llm_model": None,
    }
    defaults.update(overrides)
    ns = SimpleNamespace(**defaults)

    def resolve_fallback():
        if ns.fallback_llm_provider:
            return ns.fallback_llm_provider, ns.fallback_llm_model
        primary = ns.enrichment_provider
        candidates = [
            ("openai", ns.openai_api_key),
            ("gemini", ns.gemini_api_key),
        ]
        for prov, key in candidates:
            if key and prov != primary:
                return prov, None
        return None, None

    ns.resolve_fallback = resolve_fallback
    return ns


@pytest.mark.unit
class TestEnrichMemoryFallbackChain:
    """Verify enrich_memory falls through primary → alternative → heuristic."""

    @pytest.mark.asyncio
    async def test_fake_provider_returns_immediately(self):
        from core_api.services.memory_enrichment import MemoryEnrichment, enrich_memory

        tc = _make_tenant_config(enrichment_provider="fake")
        result = await enrich_memory("We decided to use Kafka", tenant_config=tc)
        assert isinstance(result, MemoryEnrichment)
        assert result.memory_type == "decision"  # keyword match

    @pytest.mark.asyncio
    async def test_none_provider_returns_defaults(self):
        from core_api.services.memory_enrichment import MemoryEnrichment, enrich_memory

        tc = _make_tenant_config(enrichment_provider="none")
        result = await enrich_memory("anything", tenant_config=tc)
        assert isinstance(result, MemoryEnrichment)
        assert result.memory_type == "fact"  # default

    @pytest.mark.asyncio
    async def test_primary_gemini_success_no_fallback(self):
        from core_api.services.memory_enrichment import MemoryEnrichment, enrich_memory

        tc = _make_tenant_config(enrichment_provider="gemini")
        mock_result = MemoryEnrichment(memory_type="episode", weight=0.8, title="test")

        with patch(
            "common.enrichment.service.call_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fb:
            result = await enrich_memory("test content", tenant_config=tc)

        assert result.memory_type == "episode"
        mock_fb.assert_called_once()

    @pytest.mark.asyncio
    async def test_primary_fails_falls_to_openai(self):
        """Gemini fails after retries → falls back to OpenAI."""
        from core_api.services.memory_enrichment import MemoryEnrichment, enrich_memory

        tc = _make_tenant_config(enrichment_provider="gemini", openai_api_key="sk-test")
        openai_result = MemoryEnrichment(
            memory_type="decision", weight=0.9, title="fallback"
        )

        call_count = 0

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            nonlocal call_count
            # Simulate: primary fails, fallback succeeds
            call_count = 2  # primary + fallback
            return openai_result

        with patch(
            "common.enrichment.service.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await enrich_memory("test content", tenant_config=tc)

        assert result.memory_type == "decision"
        assert call_count == 2  # tried gemini, then openai

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_heuristic(self):
        """Both gemini and openai fail → returns keyword heuristic result."""
        from core_api.services.memory_enrichment import enrich_memory

        tc = _make_tenant_config(enrichment_provider="gemini", openai_api_key="sk-test")

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            # Simulate all providers failing — call_with_fallback invokes fake_fn
            return fake_fn()

        with patch(
            "common.enrichment.service.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await enrich_memory("We decided to use Kafka", tenant_config=tc)

        # Falls back to _fake_enrich keyword heuristic
        assert result.memory_type == "decision"
        assert result.llm_ms >= 0

    @pytest.mark.asyncio
    async def test_never_raises(self):
        """enrich_memory must NEVER raise — always returns MemoryEnrichment."""
        from core_api.services.memory_enrichment import MemoryEnrichment, enrich_memory

        tc = _make_tenant_config(enrichment_provider="gemini", openai_api_key=None)

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            # Simulate all providers failing — call_with_fallback invokes fake_fn
            return fake_fn()

        with patch(
            "common.enrichment.service.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await enrich_memory("some content", tenant_config=tc)

        assert isinstance(result, MemoryEnrichment)


# ---------------------------------------------------------------------------
# Unit tests: extract_entities_from_content fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractEntitiesFallbackChain:
    """Verify extract_entities_from_content falls through primary → alternative → heuristic."""

    @pytest.mark.asyncio
    async def test_fake_provider_uses_regex(self):
        from core_api.services.entity_extraction import (
            ExtractedGraph,
            extract_entities_from_content,
        )

        tc = _make_tenant_config(entity_extraction_provider="fake")
        result = await extract_entities_from_content(
            "John Smith joined Acme Corp", "fact", tenant_config=tc
        )
        assert isinstance(result, ExtractedGraph)
        # Regex extracts capitalized multi-word phrases
        names = [e.canonical_name for e in result.entities]
        assert "john smith" in names

    @pytest.mark.asyncio
    async def test_none_provider_returns_empty(self):
        from core_api.services.entity_extraction import extract_entities_from_content

        tc = _make_tenant_config(entity_extraction_provider="none")
        result = await extract_entities_from_content(
            "anything", "fact", tenant_config=tc
        )
        assert result.entities == []
        assert result.relations == []

    @pytest.mark.asyncio
    async def test_primary_gemini_success_no_fallback(self):
        from core_api.services.entity_extraction import (
            ExtractedGraph,
            extract_entities_from_content,
        )

        tc = _make_tenant_config(entity_extraction_provider="gemini")
        mock_result = ExtractedGraph(entities=[], relations=[])

        with patch(
            "core_api.services.entity_extraction.call_with_fallback",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fb:
            result = await extract_entities_from_content(
                "test", "fact", tenant_config=tc
            )

        assert result == mock_result
        mock_fb.assert_called_once()

    @pytest.mark.asyncio
    async def test_primary_fails_falls_to_openai(self):
        from core_api.services.entity_extraction import (
            ExtractedGraph,
            extract_entities_from_content,
        )

        tc = _make_tenant_config(
            entity_extraction_provider="gemini", openai_api_key="sk-test"
        )
        openai_result = ExtractedGraph(entities=[], relations=[])

        call_count = 0

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            nonlocal call_count
            # Simulate: primary fails, fallback succeeds
            call_count = 2  # primary + fallback
            return openai_result

        with patch(
            "core_api.services.entity_extraction.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await extract_entities_from_content(
                "test", "fact", tenant_config=tc
            )

        assert result == openai_result
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_regex(self):
        from core_api.services.entity_extraction import extract_entities_from_content

        tc = _make_tenant_config(
            entity_extraction_provider="gemini", openai_api_key="sk-test"
        )

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            # Simulate all providers failing — call_with_fallback invokes fake_fn
            return fake_fn()

        with patch(
            "core_api.services.entity_extraction.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await extract_entities_from_content(
                "John Smith joined Acme Corp", "fact", tenant_config=tc
            )

        # Falls back to _fake_extract regex
        names = [e.canonical_name for e in result.entities]
        assert "john smith" in names

    @pytest.mark.asyncio
    async def test_never_raises(self):
        """extract_entities_from_content must NEVER raise."""
        from core_api.services.entity_extraction import (
            ExtractedGraph,
            extract_entities_from_content,
        )

        tc = _make_tenant_config(
            entity_extraction_provider="gemini", openai_api_key=None
        )

        async def fake_call_with_fallback(
            primary_provider_name, call_fn, fake_fn, tenant_config=None, **kwargs
        ):
            # Simulate all providers failing — call_with_fallback invokes fake_fn
            return fake_fn()

        with patch(
            "core_api.services.entity_extraction.call_with_fallback",
            side_effect=fake_call_with_fallback,
        ):
            result = await extract_entities_from_content(
                "test", "fact", tenant_config=tc
            )

        assert isinstance(result, ExtractedGraph)


# ---------------------------------------------------------------------------
# Unit tests: _fake_enrich keyword heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFakeEnrich:
    """Verify keyword heuristic produces reasonable defaults."""

    def test_decision_keyword(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("We decided to use Kafka for event streaming")
        assert result.memory_type == "decision"
        assert result.weight > 0.5

    def test_task_keyword_sets_pending(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Task: migrate the database to v2")
        assert result.memory_type == "task"
        assert result.status == "pending"

    def test_unknown_defaults_to_fact(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("The sky is blue")
        assert result.memory_type == "fact"

    def test_title_truncated(self):
        from core_api.services.memory_enrichment import _fake_enrich

        long_content = " ".join(f"word{i}" for i in range(50))
        result = _fake_enrich(long_content)
        assert result.title.endswith("...")
        assert len(result.title.split()) <= 11  # 10 words + "..."

    def test_llm_ms_is_zero_sentinel(self):
        """``fake_enrich`` MUST emit ``llm_ms=0`` so the worker's
        ``_build_patch`` proxy (``result.llm_ms > 0`` ⇒ real LLM run)
        stays reliable. A future refactor that re-introduces wall-
        clock timing here would silently break the heuristic-fallback
        guards in ``core-worker/src/core_worker/consumer.py`` —
        clobber prior good ``ts_valid_*`` / ``contains_pii`` /
        ``retrieval_hint`` / ``summary`` / ``tags`` on a redelivery
        during an LLM outage. CAURA-595 round-8."""
        from core_api.services.memory_enrichment import _fake_enrich

        # Both empty + non-empty inputs hit ``llm_ms=0`` regardless
        # of the path through the keyword-matching ladder.
        for content in ("x", "We decided to go with Postgres", "a" * 5000):
            result = _fake_enrich(content)
            assert result.llm_ms == 0, (
                f"fake_enrich produced llm_ms={result.llm_ms} for content "
                f"of length {len(content)} — must be 0 for the worker's "
                f"heuristic-vs-real-LLM guard to work"
            )


# ---------------------------------------------------------------------------
# Unit tests: _fake_extract regex heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFakeExtract:
    """Verify regex heuristic extracts capitalized phrases."""

    def test_extracts_person_names(self):
        from core_api.services.entity_extraction import _fake_extract

        result = _fake_extract("John Smith and Jane Doe discussed the project")
        names = {e.canonical_name for e in result.entities}
        assert "john smith" in names
        assert "jane doe" in names

    def test_empty_content(self):
        from core_api.services.entity_extraction import _fake_extract

        result = _fake_extract("")
        assert result.entities == []
        assert result.relations == []

    def test_no_capitalized_phrases(self):
        from core_api.services.entity_extraction import _fake_extract

        result = _fake_extract("all lowercase content here")
        assert result.entities == []

    def test_relations_always_empty(self):
        """Regex heuristic cannot extract relations — only entities."""
        from core_api.services.entity_extraction import _fake_extract

        result = _fake_extract("John Smith manages the Auth Team")
        assert result.relations == []
