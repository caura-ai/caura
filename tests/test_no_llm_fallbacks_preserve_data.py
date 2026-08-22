"""A no-LLM fallback must not destroy data it cannot replace.

Two fallbacks produced plausible stand-in output that their callers then treated as
a real result — and both callers RETIRE the previous data as part of persisting:

* ``crystallizer_service`` creates a memory per returned fact, then ARCHIVES every
  source memory in the cluster. ``_crystallize_fake`` returns a verbatim copy of
  the cluster's highest-weight memory, so an outage archived N memories and left
  one duplicate behind.
* ``insights_service`` transitions the tenant's prior insights to ``outdated``
  before persisting, guarded by ``if findings:``. ``_fake_insights`` returns one
  finding, so an outage retired real insights in favour of "Fake insight for
  testing".

Both callers already had a correct "nothing came back" path. The fix is to take it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core_api.services.crystallizer_service import (
    _crystallize_cluster,
    _crystallize_fake,
)
from core_api.services.insights_service import _fake_insights

pytestmark = [pytest.mark.unit]

_MEMORIES = [
    {"id": "m1", "content": "Helios shipped on the 3rd", "memory_type": "fact", "weight": 0.9},
    {"id": "m2", "content": "Anna owns the rollback plan", "memory_type": "fact", "weight": 0.4},
]


def _outage_config():
    """A configured REAL provider whose key is missing — resolves to
    FakeLLMProvider and lands on ``fake_fn``. The production shape."""
    config = MagicMock()
    config.enrichment_provider = "openai"
    config.enrichment_model = None
    config.openai_api_key = None
    config.anthropic_api_key = None
    config.gemini_api_key = None
    config.openrouter_api_key = None
    return config


def _fake_config():
    config = MagicMock()
    config.enrichment_provider = "fake"
    config.enrichment_model = None
    return config


# ── crystallizer ──────────────────────────────────────────────────────────────


def test_the_stand_in_really_would_have_returned_a_copy() -> None:
    """Control: proves the outage assertion below is not vacuous."""
    out = _crystallize_fake(_MEMORIES)

    assert len(out) == 1
    assert out[0]["content"] == "Helios shipped on the 3rd", (
        "the stand-in returns the highest-weight memory verbatim — that is the "
        "behaviour that must not reach production"
    )


@pytest.mark.asyncio
async def test_crystallizer_returns_nothing_on_an_outage() -> None:
    """Empty means the caller's ``if not extracted: continue`` fires, so nothing is
    created and — the point — nothing is archived."""
    out = await _crystallize_cluster(_MEMORIES, _outage_config())

    assert out == [], (
        f"a non-empty result archives every source memory in the cluster; got {out!r}"
    )


@pytest.mark.asyncio
async def test_crystallizer_keeps_the_stand_in_for_a_deliberate_fake_provider() -> None:
    """The counterpart: asking for ``fake`` still yields the stand-in, so the
    dev/CI path that exercises create + archive end to end still works."""
    out = await _crystallize_cluster(_MEMORIES, _fake_config())

    assert len(out) == 1
    assert out[0]["content"] == "Helios shipped on the 3rd"


# ── insights ──────────────────────────────────────────────────────────────────


def test_the_placeholder_finding_really_is_non_empty() -> None:
    """Control for the insights outage assertion."""
    assert _fake_insights()["findings"], "placeholder must be non-empty to be dangerous"


@pytest.mark.asyncio
async def test_insights_returns_no_findings_on_an_outage() -> None:
    """Empty findings skip the supersede, so prior insights stay live.

    Driven through the public analyse entrypoint rather than the fallback, so this
    would still catch a caller that stopped honouring the empty case.
    """
    from core_api.services.insights_service import _run_llm_analysis

    result = await _run_llm_analysis("some prompt", _outage_config())

    assert result["findings"] == [], (
        f"any finding here outdates the tenant's prior insights; got {result!r}"
    )


@pytest.mark.asyncio
async def test_insights_keeps_the_placeholder_for_a_deliberate_fake_provider() -> None:
    from core_api.services.insights_service import _run_llm_analysis

    result = await _run_llm_analysis("some prompt", _fake_config())

    assert len(result["findings"]) == 1
