"""A no-LLM recall summary must not pass itself off as synthesis.

The last `fake_fn` site from the sweep behind #821 / #822 that is safe to fix in
isolation. Unlike those two, ``recall_service`` neither persists its result nor
retires anything — the string lands in the response's ``summary`` and is surfaced
verbatim. So returning something beats returning nothing; it just has to say what
it is, rather than passing three truncated fragments off as an answer.

(``evolve_service._fake_rule`` is the remaining site. It fabricates a
``memory_type="rule"`` memory, but abstaining there turns on what provider
``"none"`` should mean, and the test suite's default is ``none`` — see the PR.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


pytestmark = [pytest.mark.unit]


class _Mem(SimpleNamespace):
    """The attribute surface ``summarize_memories`` actually touches: the prompt
    formatter's fields, ``ts_valid_start`` for its sort, and ``model_dump`` for the
    response assembly. A MagicMock cannot stand in — its auto-attributes are truthy
    but not orderable, so the sort raises before the fallback is ever reached."""

    def model_dump(self, mode: str | None = None) -> dict:
        return {"content": self.content, "memory_type": self.memory_type}


def _mem(content: str) -> _Mem:
    return _Mem(
        content=content, memory_type="fact", title=None, status="active", ts_valid_start=None
    )


def _outage_config():
    config = MagicMock()
    config.enrichment_provider = "openai"
    config.enrichment_model = None
    config.openai_api_key = None
    config.anthropic_api_key = None
    config.gemini_api_key = None
    config.openrouter_api_key = None
    return config


# ── recall: say what it is ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_summary_says_it_is_unsynthesized() -> None:
    """The summary is surfaced verbatim to the caller, so the marker has to be in
    the text — a side-channel flag nobody reads is the same as no flag."""
    from core_api.services.recall_service import summarize_memories

    # SimpleNamespace, not MagicMock: summarize_memories sorts on
    # ``ts_valid_start`` and a MagicMock's auto-attribute is truthy but not
    # orderable, so mocks blow up in the sort before reaching the fallback.
    result = await summarize_memories(
        [
            _mem("Helios shipped on the 3rd"),
            _mem("Anna owns the rollback plan"),
        ],
        "what happened with helios?",
        _outage_config(),
    )
    summary = result["summary"]

    assert "LLM unavailable" in summary, (
        f"three truncated fragments must not read as a synthesised answer; got {summary!r}"
    )
    # Still returns the content — this is a read path, so degraded beats empty.
    assert "Helios shipped" in summary
