"""Regression tests for the prompt-building brace bug.

``str.format`` parses only the TEMPLATE for replacement fields — a substituted
value is inserted literally and never re-scanned, so it never raises KeyError and
must NOT be brace-escaped. Pre-escaping (``content.replace("{", "{{")``) corrupted
any brace-containing content (JSON, code, dict reprs) before it reached the LLM.
These guard the three call sites that had the bug: ``enrich_memory`` (calling-code
capture) and the evolve / insights prompt templates (invariant).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Content that would be corrupted by escaping: JSON + a bare {field}-looking token.
_BRACED = 'deploy config = {"replicas": 3}; for {i} in range(3): log({i})'


@pytest.mark.asyncio
async def test_enrich_memory_prompt_preserves_braces():
    """enrich_memory must send brace-containing content to the LLM verbatim."""
    from core_api.services.memory_enrichment import enrich_memory

    captured: list[str] = []

    async def fake_complete_json(prompt: str):
        captured.append(prompt)
        return {
            "memory_type": "fact",
            "weight": 0.5,
            "title": "t",
            "summary": "s",
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
        await enrich_memory(_BRACED, tenant_config)

    assert captured, "LLM was not called"
    # Verbatim content in the prompt — not the corrupted {{...}} form the old
    # brace-escaping produced (which would make this substring check fail).
    assert _BRACED in captured[0]


def test_rule_generation_template_preserves_braces():
    """The evolve rule-generation template preserves braces in substituted values."""
    from core_api.services.evolve_service import _RULE_GENERATION_PROMPT

    out = _RULE_GENERATION_PROMPT.format(
        outcome="set {x} and {y}",
        outcome_type="success",
        memories='cfg = {"a": 1}',
        count=1,
    )
    assert "set {x} and {y}" in out
    assert 'cfg = {"a": 1}' in out


def test_insights_templates_preserve_braces():
    """Every insights analysis template preserves braces (cluster mode passes
    Python dict reprs; content mode can carry user-controlled {...} strings)."""
    from core_api.services.insights_service import _PROMPT_DISPATCH

    braced = "type_distribution = {'fact': 3}; note {placeholder}"
    for key, template in _PROMPT_DISPATCH.items():
        out = template.format(memories=braced, count=2)
        assert braced in out, key
