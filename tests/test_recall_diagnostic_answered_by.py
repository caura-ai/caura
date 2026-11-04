"""The recall diagnostic names the model that wrote the brief.

It used to report the tenant's resolved recall setting. That setting is not
what answers when the configured provider has no key: the registry hands back
the platform LLM and ignores ``recall_model`` (``get_llm_provider``). A tenant
configured as openai/gpt-5.4-nano on a deployment with no OpenAI key therefore
had every brief written by the platform model, while the diagnostic said
openai/gpt-5.4-nano.

These run the real ``call_with_fallback`` and registry. Only the platform LLM
is a stand-in, because constructing the real one needs cloud credentials.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import common.llm.registry as registry_mod
from core_api.openapi_responses import RecallDiagnostic
from core_api.services.recall_service import summarize_memories

pytestmark = [pytest.mark.unit]


class _PlatformLLM:
    """Stands in for the platform LLM singleton (Vertex on hosted deployments)."""

    is_fake = False
    provider_name = "vertex"
    model = "platform-model"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def complete_text(self, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("platform LLM unavailable")
        return "Step 1: the memory gives the date.\n**Answer:** month-end"


class _Mem(SimpleNamespace):
    def model_dump(self, mode: str | None = None) -> dict:
        return {"content": self.content, "memory_type": self.memory_type}


def _mem() -> _Mem:
    return _Mem(
        content="The Granite Q4 review is scheduled for month-end.",
        memory_type="fact",
        title=None,
        status="active",
        ts_valid_start=None,
    )


def _keyless_openai_config() -> SimpleNamespace:
    return SimpleNamespace(
        recall_enabled=True,
        recall_provider="openai",
        recall_model="gpt-5.4-nano",
        openai_api_key=None,
    )


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> _PlatformLLM:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = _PlatformLLM()
    monkeypatch.setattr(registry_mod, "get_platform_llm", lambda: llm)
    return llm


@pytest.mark.asyncio
async def test_a_keyless_provider_reports_the_platform_model_that_answered(
    platform: _PlatformLLM,
) -> None:
    result = await summarize_memories(
        [_mem()],
        "When is the Granite Q4 review?",
        _keyless_openai_config(),
        diagnostic=True,
    )

    assert platform.calls == 1
    assert result["summary"] == "month-end"
    diag = result["diagnostic"]
    assert (diag["recall_provider"], diag["recall_model"]) == (
        "vertex",
        "platform-model",
    )
    assert (diag["recall_configured_provider"], diag["recall_configured_model"]) == (
        "openai",
        "gpt-5.4-nano",
    )
    RecallDiagnostic.model_validate(diag)


@pytest.mark.asyncio
async def test_a_provider_that_raises_is_not_reported_as_the_author(
    platform: _PlatformLLM,
) -> None:
    """The platform call fails, no fallback provider is configured, and the
    unsynthesized fallback writes the summary. The diagnostic must name that,
    not the provider that was tried."""
    platform.fail = True

    result = await summarize_memories(
        [_mem()],
        "When is the Granite Q4 review?",
        _keyless_openai_config(),
        diagnostic=True,
    )

    assert platform.calls == 1
    assert result["summary"].startswith("(LLM unavailable")
    diag = result["diagnostic"]
    assert (diag["recall_provider"], diag["recall_model"]) == ("fake", None)
    assert diag["recall_configured_provider"] == "openai"


@pytest.mark.asyncio
async def test_a_deliberately_fake_provider_is_reported_as_fake() -> None:
    config = SimpleNamespace(
        recall_enabled=True, recall_provider="fake", recall_model=None
    )

    diag = (await summarize_memories([_mem()], "q", config, diagnostic=True))[
        "diagnostic"
    ]

    assert (diag["recall_provider"], diag["recall_model"]) == ("fake", None)
    assert diag["recall_configured_provider"] == "fake"


@pytest.mark.asyncio
async def test_no_llm_ran_when_summarization_is_disabled() -> None:
    config = SimpleNamespace(recall_enabled=False, recall_provider="openai")

    diag = (await summarize_memories([_mem()], "q", config, diagnostic=True))[
        "diagnostic"
    ]

    assert (diag["recall_provider"], diag["recall_model"]) == (None, None)
    assert diag["recall_configured_provider"] == "openai"
