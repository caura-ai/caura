"""A deployment can give chosen tenants a different recall model.

``PLATFORM_RECALL_LLM_MODEL`` and ``PLATFORM_RECALL_LLM_TENANTS`` build a second
Vertex model at startup, and recall uses it for the listed tenants wherever the
registry would have used the platform LLM. They are deployment config only: no
tenant setting reaches them. The point is to try a model on a test tenant
while every other tenant of the same deployment stays on the default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import common.llm._platform as platform_mod
import common.llm.registry as registry_mod
from common.llm.providers.vertex import VertexLLMProvider
from core_api.services.recall_service import summarize_memories

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_platform(monkeypatch: pytest.MonkeyPatch):
    """Leave the module singletons and init errors as they were found."""
    for name in (
        "_platform_llm",
        "_platform_recall_llm",
        "_platform_recall_tenants",
        "_warmup_thread",
        "_recall_warmup_thread",
    ):
        monkeypatch.setattr(platform_mod, name, getattr(platform_mod, name))
    errors = list(platform_mod._platform_init_errors)
    yield
    platform_mod._platform_init_errors[:] = errors


def _vertex_env(monkeypatch: pytest.MonkeyPatch, **recall: str) -> None:
    for var in (
        "PLATFORM_EMBEDDING_PROVIDER",
        "PLATFORM_RECALL_LLM_MODEL",
        "PLATFORM_RECALL_LLM_TENANTS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "test-proj")
    monkeypatch.setenv("PLATFORM_LLM_GCP_LOCATION", "us")
    monkeypatch.setenv("PLATFORM_LLM_MODEL", "default-model")
    for var, value in recall.items():
        monkeypatch.setenv(var, value)


# ── Startup ──────────────────────────────────────────────────────────────


def test_listed_tenants_get_the_recall_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _vertex_env(
        monkeypatch,
        PLATFORM_RECALL_LLM_MODEL="recall-model",
        PLATFORM_RECALL_LLM_TENANTS=" tenant-b , tenant-c ",
    )
    platform_mod.init_platform_providers()

    llm = platform_mod.get_platform_recall_llm("tenant-b")
    assert isinstance(llm, VertexLLMProvider)
    assert (llm.model, llm._project_id, llm._location) == (
        "recall-model",
        "test-proj",
        "us",
    )
    assert platform_mod.get_platform_recall_llm("tenant-c") is llm
    assert platform_mod.get_platform_recall_llm("tenant-a") is None
    assert platform_mod.get_platform_recall_llm(None) is None
    # The platform LLM itself is untouched.
    assert platform_mod.get_platform_llm().model == "default-model"


def test_unset_means_no_tenant_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _vertex_env(monkeypatch)
    platform_mod.init_platform_providers()

    assert platform_mod.get_platform_recall_llm("tenant-b") is None
    assert not any("recall" in e for e in platform_mod.get_platform_init_errors())


@pytest.mark.parametrize(
    "recall",
    [
        {"PLATFORM_RECALL_LLM_MODEL": "recall-model"},
        {"PLATFORM_RECALL_LLM_TENANTS": "tenant-b"},
    ],
)
def test_half_configured_is_reported_and_ignored(
    monkeypatch: pytest.MonkeyPatch, recall: dict[str, str]
) -> None:
    _vertex_env(monkeypatch, **recall)
    platform_mod.init_platform_providers()

    assert platform_mod.get_platform_recall_llm("tenant-b") is None
    assert "recall-llm-config" in platform_mod.get_platform_init_errors()


def test_it_needs_the_vertex_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _vertex_env(
        monkeypatch,
        PLATFORM_RECALL_LLM_MODEL="recall-model",
        PLATFORM_RECALL_LLM_TENANTS="tenant-b",
    )
    monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "")
    platform_mod.init_platform_providers()

    assert platform_mod.get_platform_recall_llm("tenant-b") is None
    assert "recall-llm-config" in platform_mod.get_platform_init_errors()


# ── Recall ───────────────────────────────────────────────────────────────


class _Stub:
    is_fake = False

    def __init__(self, provider_name: str, model: str) -> None:
        self.provider_name = provider_name
        self.model = model
        self.calls = 0

    async def complete_text(self, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        return "**Answer:** month-end"


class _Mem(SimpleNamespace):
    def model_dump(self, mode: str | None = None) -> dict:
        return {"content": self.content, "memory_type": self.memory_type}


_MEM = _Mem(
    content="The review is at month-end.",
    memory_type="fact",
    title=None,
    status="active",
    ts_valid_start=None,
)


def _keyless_config() -> SimpleNamespace:
    return SimpleNamespace(
        recall_enabled=True,
        recall_provider="openai",
        recall_model="gpt-5.4-nano",
        openai_api_key=None,
    )


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> tuple[_Stub, _Stub]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    default, recall = _Stub("vertex", "default-model"), _Stub("vertex", "recall-model")
    monkeypatch.setattr(platform_mod, "_platform_llm", default)
    monkeypatch.setattr(platform_mod, "_platform_recall_llm", recall)
    monkeypatch.setattr(
        platform_mod, "_platform_recall_tenants", frozenset({"tenant-b"})
    )
    return default, recall


async def _answered_by(tenant_id: str | None, config: SimpleNamespace) -> tuple:
    diag = (
        await summarize_memories(
            [_MEM], "When is the review?", config, diagnostic=True, tenant_id=tenant_id
        )
    )["diagnostic"]
    return diag["recall_provider"], diag["recall_model"]


@pytest.mark.asyncio
async def test_a_listed_tenant_is_answered_by_the_recall_model(stubs) -> None:
    default, recall = stubs
    assert await _answered_by("tenant-b", _keyless_config()) == (
        "vertex",
        "recall-model",
    )
    assert (default.calls, recall.calls) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", ["tenant-a", None])
async def test_any_other_tenant_keeps_the_platform_model(stubs, tenant_id) -> None:
    default, recall = stubs
    assert await _answered_by(tenant_id, _keyless_config()) == (
        "vertex",
        "default-model",
    )
    assert (default.calls, recall.calls) == (1, 0)


@pytest.mark.asyncio
async def test_a_tenant_on_its_own_key_keeps_it(
    stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substitution covers only what the platform serves. A listed tenant
    whose configured provider resolves to its own key is not switched."""
    default, recall = stubs
    own = _Stub("openai", "gpt-5.4-mini")
    monkeypatch.setattr(registry_mod, "get_llm_provider", lambda *a, **k: own)

    assert await _answered_by("tenant-b", _keyless_config()) == (
        "openai",
        "gpt-5.4-mini",
    )
    assert (default.calls, recall.calls, own.calls) == (0, 0, 1)
