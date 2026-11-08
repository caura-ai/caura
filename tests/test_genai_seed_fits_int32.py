"""The seed sent to Vertex and Gemini fits their signed 32-bit field.

Entity extraction pins its seed to ``zlib.crc32`` of the prompt: an unsigned
32-bit value, 0 to 2**32 - 1. ``GenerateContentConfig.seed`` is a signed int32
on the wire, so whenever the CRC was 2**31 or more, Vertex refused the request
with ``400 INVALID_ARGUMENT: Invalid value at 'generation_config.seed'
(TYPE_INT32)``. The retry sent the same seed, no fallback provider was
configured, and the extraction stored the regex heuristic.

That is about half of all prompts. On 2026-11-07, 531 of production's 533
heuristic extractions were this refusal. The first came at 2026-10-10 23:06
UTC, on the first deploy after L-95 (#2019) began sending the seed to the
Gemini-backed providers. The L-95 tests used ``seed=1234``, which fits, so none
of them could see it.
"""

from __future__ import annotations

import json
import logging
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import errors as genai_errors

from common.llm.providers.gemini import GeminiLLMProvider
from common.llm.providers.vertex import VertexLLMProvider
from core_api.services import entity_extraction as ee

pytestmark = [pytest.mark.unit]

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1

_GRAPH = {
    "entities": [
        {
            "canonical_name": "anna bergstrom",
            "entity_type": "person",
            "role": "subject",
        },
        {
            "canonical_name": "acme corp",
            "entity_type": "organization",
            "role": "object",
        },
    ],
    "relations": [
        {
            "from_entity": "anna bergstrom",
            "relation_type": "works_at",
            "to_entity": "acme corp",
        }
    ],
    "mentions": [],
}


class _Models:
    """``generate_content`` that refuses an out-of-range seed as the API does."""

    def __init__(self) -> None:
        self.seeds: list[int | None] = []

    def generate_content(self, *, model, contents, config):
        self.seeds.append(config.seed)
        if config.seed is not None and not _INT32_MIN <= config.seed <= _INT32_MAX:
            raise genai_errors.ClientError(
                400,
                {
                    "error": {
                        "code": 400,
                        "message": "Invalid value at 'generation_config.seed' (TYPE_INT32), ",
                        "status": "INVALID_ARGUMENT",
                    }
                },
                None,
            )
        return SimpleNamespace(
            text=json.dumps(_GRAPH),
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


def _content_whose_seed_needs_32_bits() -> str:
    """A memory whose extraction prompt has a CRC32 of 2**31 or more.

    Searched rather than fixed, so a change to the prompt's wording cannot
    quietly turn this into a test of a seed that always fit.
    """
    for n in range(1000):
        content = f"Anna Bergstrom works at Acme Corp. (note {n})"
        prompt = ee.EXTRACTION_PROMPT.format(memory_type="fact", content=content)
        if zlib.crc32(prompt.encode("utf-8")) > _INT32_MAX:
            return content
    raise AssertionError("no candidate content has a 32-bit seed")


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_vertex, _gemini], ids=["vertex", "gemini"])
async def test_an_extraction_whose_seed_needs_32_bits_reaches_the_model(
    make, monkeypatch, caplog
):
    provider = make()
    monkeypatch.setattr(
        "common.llm.registry.get_llm_provider", lambda *_a, **_k: provider
    )
    monkeypatch.setattr(ee.settings, "entity_extraction_provider", "openai")
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with caplog.at_level(logging.ERROR, logger=ee.__name__):
        graph = await ee.extract_entities_from_content(
            _content_whose_seed_needs_32_bits(), "fact"
        )

    assert not [
        r
        for r in caplog.records
        if "entity_extraction_degraded_to_heuristic" in r.getMessage()
    ]
    assert {(e.canonical_name, e.entity_type) for e in graph.entities} == {
        ("anna bergstrom", "person"),
        ("acme corp", "organization"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_vertex, _gemini], ids=["vertex", "gemini"])
@pytest.mark.parametrize("seed", [2**31, 2**32 - 1, -(2**31) - 1, 2**63])
async def test_a_seed_outside_int32_is_sent_inside_it_and_the_same_every_time(
    make, seed
):
    provider = make()

    await provider.complete_json("prompt", seed=seed)
    await provider.complete_json("prompt", seed=seed)

    first, second = provider._client.models.seeds
    assert _INT32_MIN <= first <= _INT32_MAX
    assert first == second


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_vertex, _gemini], ids=["vertex", "gemini"])
@pytest.mark.parametrize("seed", [0, 1234, _INT32_MAX, -1, _INT32_MIN])
async def test_a_seed_that_fits_is_sent_unchanged(make, seed):
    """Prompts that worked before keep exactly the answer they were getting."""
    provider = make()

    await provider.complete_json("prompt", seed=seed)

    assert provider._client.models.seeds == [seed]
