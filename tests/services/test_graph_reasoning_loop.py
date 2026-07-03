"""Tool-selection and loop-orchestration behavior for the reasoning loop (RL-05).

Exercises run_reasoning_loop() in isolation from the storage layer: the four
typed dispatch targets (query_by_keyword, query_by_graph_context,
query_by_time_range, query_by_entity_links) are monkeypatched directly on the
graph_reasoning_loop module, since RL-01 already covers their correctness
against real storage. Only the LLM tool-choice call is exercised for real,
via a scripted provider injected through common.llm.registry.get_llm_provider
(the lazy-import seam call_with_fallback uses when no provider_factory is
passed) — so no PostgreSQL fixture is required here, unlike RL-04.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from core_api.services import graph_reasoning_loop
from core_api.services.graph_reasoning_loop import (
    GRAPH_REASONING_MAX_ITERATIONS,
    run_reasoning_loop,
)

TENANT_ID = "test-tenant"
FLEET_IDS = ["test-fleet"]


class _ScriptedProvider:
    """Implements the LLMProvider protocol, returning a queued sequence of tool choices."""

    is_fake = False
    provider_name = "test-scripted"
    model = "test-model"

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)

    async def complete_json(self, prompt, *, temperature=0.0, seed=None, response_schema=None) -> dict:
        if not self._responses:
            return {"tool": "done", "args": {}}
        return self._responses.pop(0)

    async def complete_text(self, prompt, *, temperature=0.3, max_tokens=1000) -> str:
        return ""


class _Config:
    """Minimal tenant-config double: non-fake provider, no resolve_fallback."""

    recall_provider = "openai"
    recall_enabled = True


def _patch_provider(monkeypatch, responses: list[dict]) -> None:
    provider = _ScriptedProvider(responses)

    def _factory(name, tenant_config, *, model_override=None, model_attr="enrichment_model"):
        return provider

    monkeypatch.setattr("common.llm.registry.get_llm_provider", _factory)


def _fail_if_called(name: str):
    async def _unexpected(*args, **kwargs):
        raise AssertionError(f"{name} should not have been dispatched in this test")

    return _unexpected


@pytest.mark.asyncio
async def test_temporal_query_picks_time_range(monkeypatch):
    """A temporal-phrased query drives the loop to pick query_by_time_range."""
    _patch_provider(
        monkeypatch,
        [{"tool": "query_by_time_range", "args": {}}, {"tool": "done", "args": {}}],
    )

    memory_id = uuid4()

    async def _fake_time_range(query, tenant_id, fleet_ids=None, reference_datetime=None):
        return {memory_id}, set(), "found 1 memory in range"

    monkeypatch.setattr(graph_reasoning_loop, "query_by_time_range", _fake_time_range)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_keyword", _fail_if_called("query_by_keyword"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_graph_context", _fail_if_called("query_by_graph_context"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_entity_links", _fail_if_called("query_by_entity_links"))

    boosted_ids, boost_factor, trace = await run_reasoning_loop(
        "what happened three weeks ago", TENANT_ID, _Config(), seed_entity_ids=[], fleet_ids=FLEET_IDS
    )

    assert boosted_ids == {str(memory_id)}
    assert boost_factor == 1.1
    assert trace[0]["tool"] == "query_by_time_range"
    assert trace[0]["result_count"] == 1


@pytest.mark.asyncio
async def test_relational_query_picks_graph_context(monkeypatch):
    """A relational-phrased query drives the loop to pick query_by_graph_context."""
    _patch_provider(
        monkeypatch,
        [{"tool": "query_by_graph_context", "args": {}}, {"tool": "done", "args": {}}],
    )

    seed_entity_id = uuid4()
    linked_entity_id = uuid4()
    memory_id = uuid4()

    async def _fake_graph_context(seed_entity_ids, tenant_id, fleet_ids=None, max_hops=2):
        assert seed_entity_ids == [seed_entity_id]
        return {memory_id}, {linked_entity_id}, "expanded 1 seed entity to 1 entity"

    monkeypatch.setattr(graph_reasoning_loop, "query_by_graph_context", _fake_graph_context)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_keyword", _fail_if_called("query_by_keyword"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_time_range", _fail_if_called("query_by_time_range"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_entity_links", _fail_if_called("query_by_entity_links"))

    boosted_ids, boost_factor, trace = await run_reasoning_loop(
        "what is X's role with Y",
        TENANT_ID,
        _Config(),
        seed_entity_ids=[str(seed_entity_id)],
        fleet_ids=FLEET_IDS,
    )

    assert boosted_ids == {str(memory_id)}
    assert boost_factor == 1.1
    assert trace[0]["tool"] == "query_by_graph_context"


@pytest.mark.asyncio
async def test_loop_terminates_at_max_iterations(monkeypatch):
    """The loop stops after GRAPH_REASONING_MAX_ITERATIONS turns even if 'done' is never chosen."""
    responses = [
        {"tool": "query_by_keyword", "args": {}},
        {"tool": "query_by_graph_context", "args": {}},
        {"tool": "query_by_entity_links", "args": {}},
        {"tool": "query_by_time_range", "args": {}},  # never reached — loop caps at 3 turns
    ]
    _patch_provider(monkeypatch, responses)

    async def _nonempty(*args, **kwargs):
        return {uuid4()}, {uuid4()}, "nonempty result, keeps the loop going"

    monkeypatch.setattr(graph_reasoning_loop, "query_by_keyword", _nonempty)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_graph_context", _nonempty)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_entity_links", _nonempty)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_time_range", _fail_if_called("query_by_time_range"))

    _boosted_ids, _boost_factor, trace = await run_reasoning_loop(
        "tell me everything", TENANT_ID, _Config(), seed_entity_ids=[], fleet_ids=FLEET_IDS
    )

    assert len(trace) == GRAPH_REASONING_MAX_ITERATIONS == 3
    assert [t["tool"] for t in trace] == ["query_by_keyword", "query_by_graph_context", "query_by_entity_links"]
    assert "done" not in [t["tool"] for t in trace]


@pytest.mark.asyncio
async def test_provider_error_returns_empty_no_raise(monkeypatch):
    """A dispatch-time error degrades to an empty result instead of raising."""
    _patch_provider(monkeypatch, [{"tool": "query_by_time_range", "args": {}}])

    async def _boom(*args, **kwargs):
        raise RuntimeError("storage layer exploded")

    monkeypatch.setattr(graph_reasoning_loop, "query_by_time_range", _boom)
    monkeypatch.setattr(graph_reasoning_loop, "query_by_keyword", _fail_if_called("query_by_keyword"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_graph_context", _fail_if_called("query_by_graph_context"))
    monkeypatch.setattr(graph_reasoning_loop, "query_by_entity_links", _fail_if_called("query_by_entity_links"))

    boosted_ids, boost_factor, trace = await run_reasoning_loop(
        "what happened three weeks ago", TENANT_ID, _Config(), seed_entity_ids=[], fleet_ids=FLEET_IDS
    )

    assert boosted_ids == set()
    assert boost_factor == 0.0
    assert trace == [{"turn": 0, "tool": None, "args": {}, "result_count": 0}]
