"""L-95: the Gemini-backed providers send the caller's seed.

Entity extraction pins a seed (a CRC32 of its prompt) so that a re-ask returns
the same answer, and on that basis does not retry a shape failure
(``call_with_retry(non_retryable=...)``). Vertex, the prod provider, and Gemini
accepted ``seed`` and dropped it, so on them that rationale was false.
``GenerateContentConfig`` takes a ``seed``; both providers now pass it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from common.llm.providers.gemini import GeminiLLMProvider
from common.llm.providers.vertex import VertexLLMProvider

pytestmark = [pytest.mark.unit]


class _Models:
    """Records the config ``generate_content`` was called with."""

    def __init__(self):
        self.last_config = None

    def generate_content(self, *, model, contents, config):
        self.last_config = config
        return SimpleNamespace(
            text=json.dumps({"ok": True}),
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )


def _vertex():
    p = VertexLLMProvider(project_id="test-proj", location="us-central1", model="m")
    p._client = SimpleNamespace(models=_Models())
    return p


def _gemini():
    p = GeminiLLMProvider(api_key="AIza-test", model="m")
    p._client = SimpleNamespace(models=_Models())
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_vertex, _gemini], ids=["vertex", "gemini"])
async def test_the_seed_reaches_generate_content(make):
    provider = make()
    assert await provider.complete_json("prompt", seed=1234) == {"ok": True}
    assert provider._client.models.last_config.seed == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_vertex, _gemini], ids=["vertex", "gemini"])
async def test_no_seed_leaves_it_unset(make):
    provider = make()
    await provider.complete_json("prompt")
    assert provider._client.models.last_config.seed is None
