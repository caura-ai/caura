"""The crystallizer's answer has to survive ``complete_json``.

``complete_json`` returns a dict on every provider (CAURA-651): a
multi-element JSON array raises a ``ResponseShapeError`` and a one-element one
is unwrapped to its dict. The crystallization prompt asked for a bare array
and ``_crystallize_cluster`` accepted only a list, so every answer the model
could give was lost. Several facts raised, one fact arrived as a dict and was
refused, and ``[]`` raised. Every cluster was skipped: production ran 88
crystallizations between 2026-10-01 and 2026-11-06 and each one crystallized
0 -> 0.

The tests that existed mocked ``complete_json`` returning a list, which no
provider returns, so none of them could see it. These run the answer through
the real Vertex provider's parsing, with only the network faked.
"""

from __future__ import annotations

import enum
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.llm.providers.vertex import VertexLLMProvider
from core_api.services import crystallizer_service as cs

pytestmark = pytest.mark.unit


class _FinishReason(enum.Enum):
    STOP = 1


class _Models:
    def __init__(self, text: str) -> None:
        self._text = text

    def generate_content(self, *, model, contents, config):
        return SimpleNamespace(
            text=self._text,
            candidates=[SimpleNamespace(finish_reason=_FinishReason.STOP)],
        )


async def _crystallize_through_vertex(answer: str) -> list[dict] | None:
    """``_crystallize_cluster`` with the model answering ``answer`` verbatim."""
    provider = VertexLLMProvider(project_id="test", location="us", model="gemini-test")
    provider._client = SimpleNamespace(models=_Models(answer))

    async def run_call_fn(*_args, call_fn, **_kwargs):
        return await call_fn(provider)

    with patch.object(cs, "call_with_fallback", side_effect=run_call_fn):
        return await cs._crystallize_cluster(
            [{"content": "Billing deploys on Tuesdays.", "memory_type": "fact"}],
            SimpleNamespace(enrichment_provider="openai"),
        )


def _fill(node):
    """The prompt's example with each ``"..."`` placeholder filled in."""
    if isinstance(node, dict):
        return {key: _fill(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill(value) for value in node]
    return "fact" if node == "..." else node


async def test_the_answer_the_prompt_asks_for_comes_through():
    prompt = cs.CRYSTALLIZATION_PROMPT.format(memories="")
    example = prompt.split("Return ONLY", 1)[1].splitlines()[1]

    out = await _crystallize_through_vertex(json.dumps(_fill(json.loads(example))))

    assert out is not None, "the cluster was skipped"
    assert len(out) == 1


def _fact(content: str) -> dict:
    return {"content": content, "memory_type": "fact", "weight": 0.7}


@pytest.mark.parametrize(
    ("answer", "facts"),
    [
        ({"facts": [_fact("a")]}, 1),
        ({"facts": [_fact("a"), _fact("b")]}, 2),
        ({"facts": []}, 0),
    ],
    ids=["one fact", "several facts", "nothing worth keeping"],
)
async def test_every_size_of_answer_is_decided(answer, facts):
    out = await _crystallize_through_vertex(json.dumps(answer))

    assert out is not None, "the cluster was skipped"
    assert len(out) == facts
