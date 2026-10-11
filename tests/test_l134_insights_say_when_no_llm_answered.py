"""L-134: an insights run that no LLM answered says so.

With every provider down, insights fell back to ``_skip_insights``: no findings,
so prior insights stay live (``test_no_llm_fallbacks_preserve_data.py`` pins
that). But the result looked exactly like "nothing found": REST answered 200
with ``findings=[]`` and only an English summary to tell the two apart, and the
nightly lifecycle run returned 0 and recorded success, so an outage across the
whole discover pass left no trace in the audit trail.

Decided 2026-10-09 (Eldad): the result carries ``skipped_reason:
"llm_unavailable"``, and the nightly run records a terminal failure. It is not
redelivered; the next scheduled run tries again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.events.base import PermanentOpError
from core_api.services import insights_service
from core_api.services.lifecycle_audit import _CoreApiLifecycleAdapter

pytestmark = [pytest.mark.unit]

_MEMORIES = [
    {"id": "m1", "content": "Helios shipped on the 3rd", "memory_type": "fact"},
    {"id": "m2", "content": "Anna owns the rollback plan", "memory_type": "fact"},
]


def _outage_config():
    """A real provider with no key: every call lands on the outage stand-in."""
    config = MagicMock()
    config.enrichment_provider = "openai"
    config.enrichment_model = None
    config.openai_api_key = None
    config.anthropic_api_key = None
    config.gemini_api_key = None
    config.openrouter_api_key = None
    return config


async def _synthesize(config) -> dict:
    return await insights_service.synthesize_insights(
        _MEMORIES, False, config, focus="patterns", scope="agent"
    )


@pytest.mark.asyncio
async def test_an_outage_is_named_in_the_synthesis():
    synth = await _synthesize(_outage_config())
    assert synth["findings"] == []
    assert synth["skipped_reason"] == "llm_unavailable"


@pytest.mark.asyncio
async def test_a_model_cannot_claim_an_outage(monkeypatch):
    async def _answer(prompt, config):
        return {"findings": [], "summary": "s", "skipped_reason": "llm_unavailable"}

    monkeypatch.setattr(insights_service, "_run_llm_analysis", _answer)
    synth = await _synthesize(_outage_config())
    assert "skipped_reason" not in synth


@pytest.mark.asyncio
async def test_generate_insights_returns_the_reason(monkeypatch):
    monkeypatch.setitem(
        insights_service._QUERY_DISPATCH, "patterns", AsyncMock(return_value=_MEMORIES)
    )
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_outage_config()),
        ),
        patch.object(
            insights_service, "_persist_findings", new=AsyncMock(return_value=[])
        ),
    ):
        result = await insights_service.generate_insights("t1", focus="patterns")

    assert result["findings"] == []
    assert result["skipped_reason"] == "llm_unavailable"


@pytest.mark.asyncio
async def test_the_nightly_run_records_a_terminal_failure():
    class _Storage:
        async def insights_activity_gate(self, *, tenant_id: str, fleet_id):
            return {
                "latest_non_insight": "2026-10-09T00:00:00+00:00",
                "latest_insight": None,
            }

        async def get_agent(self, agent_id: str, tenant_id: str):
            return {"agent_id": agent_id}

    class _On:
        auto_insights_enabled = True

    skipped = {"insight_memory_ids": [], "skipped_reason": "llm_unavailable"}
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_On()),
        ),
        patch(
            "core_api.services.insights_service.generate_insights",
            new=AsyncMock(return_value=skipped),
        ),
        pytest.raises(PermanentOpError, match="llm_unavailable"),
    ):
        await _CoreApiLifecycleAdapter(_Storage()).insights(org_id="t1", fleet_id=None)
