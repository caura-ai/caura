from unittest.mock import AsyncMock

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import execute_scored_search

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    ("query", "fts_weight", "expected_order"),
    [
        ("How many paid subscriptions do I have?", 0.3, ["subscribed", "other"]),
        ("How many paid subscriptions do I have?", 0.0, ["other", "subscribed"]),
        ("How many events did I attend?", 0.3, ["other", "subscribed"]),
        ("Where can I manage subscriptions?", 0.3, ["other", "subscribed"]),
    ],
)
async def test_subscription_count_relevance(
    monkeypatch, query, fts_weight, expected_order
):
    rows = [
        {"id": "other", "score": 0.30, "content": "I attended an event last week."},
        {
            "id": "subscribed",
            "score": 0.27,
            "content": "I subscribed to a weekly publication.",
        },
    ]
    storage = AsyncMock()
    storage.scored_search.return_value = rows
    monkeypatch.setattr(execute_scored_search, "get_storage_client", lambda: storage)
    ctx = PipelineContext(
        data={
            "tenant_id": "subscription-count",
            "query": query,
            "embedding": [0.0] * 8,
            "search_params": {"top_k": 5, "fts_weight": fts_weight},
            "temporal_window": None,
            "boosted_memory_ids": [],
            "memory_boost_factor": {},
        },
    )

    await execute_scored_search.ExecuteScoredSearch().execute(ctx)

    ranked = ctx.data["raw_rows"]
    assert [row.Memory.id for row in ranked] == expected_order
    expected_score = 0.32 if expected_order[0] == "subscribed" else 0.27
    assert next(
        row.score for row in ranked if row.Memory.id == "subscribed"
    ) == pytest.approx(expected_score)
