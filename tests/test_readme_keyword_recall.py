"""Regression coverage for the keyless README write-and-search flow."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from core_api.clients.storage_client import get_storage_client
from core_api.constants import MIN_SEARCH_SIMILARITY, SEARCH_OVERFETCH_FACTOR
from core_api.services import memory_service
from core_storage_api.services.postgres_service import get_session
from sqlalchemy import text

from common.embedding import fake_embedding
from tests.conftest import get_test_auth


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_readme_keyword_match_survives_candidate_cutoff(
    client,
    monkeypatch,
    use_pipeline,
):
    """The README memory must survive storage and result candidate cuts.

    This reproduces R1's exact query and zero-floor retry. The keyless
    quickstart's local vectors give the target a low cosine, while enough
    stronger vector-only distractors fill storage's overfetched candidate
    window. The target still has independent full-text evidence: PostgreSQL
    matched every query term.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)

    tenant_id = f"test-tenant-readme-recall-{uuid.uuid4().hex[:8]}"
    headers = get_test_auth(tenant_id)[1]
    content = "Our auth service uses JWT with 15-minute expiry."
    query = "JWT expiry"
    write_response = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": "quickstart",
            "content": content,
        },
    )
    assert write_response.status_code == 201, write_response.text
    memory_id = write_response.json()["id"]

    content_embedding = fake_embedding(content)
    query_embedding = fake_embedding(query)
    cosine = sum(a * b for a, b in zip(content_embedding, query_embedding, strict=True))
    assert 0.0 <= cosine < MIN_SEARCH_SIMILARITY, (
        "the regression requires a full-text match that clears an explicit zero "
        "floor but not the default floor; "
        f"got cosine={cosine:.3f}, default={MIN_SEARCH_SIMILARITY:.3f}"
    )

    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT embedding IS NOT NULL AS embedded, "
                    "search_vector @@ plainto_tsquery('english', :query) AS fts_matches "
                    "FROM memories WHERE id = CAST(:id AS uuid)"
                ),
                {"query": query, "id": memory_id},
            )
        ).one()
    assert row.embedded is True, "the regression concerns an embedded row"
    assert row.fts_matches is True, "the regression requires a full-text match"

    top_k = 5
    distractor_count = top_k * SEARCH_OVERFETCH_FACTOR + 2
    storage = get_storage_client()
    for index in range(distractor_count):
        distractor = f"Unrelated high-cosine candidate {index} {uuid.uuid4().hex}"
        await storage.create_memory(
            {
                "tenant_id": tenant_id,
                "fleet_id": None,
                "agent_id": "retrieval-pressure",
                "memory_type": "fact",
                "content": distractor,
                "weight": 1.0,
                "embedding": query_embedding,
                "content_hash": hashlib.sha256(
                    f"{tenant_id}:None:{distractor}".encode()
                ).hexdigest(),
                "status": "active",
                "recall_count": 0,
                "visibility": "scope_team",
            }
        )

    for floor in (0.0, None):
        search_body = {"tenant_id": tenant_id, "query": query, "top_k": top_k}
        if floor is not None:
            search_body["min_similarity"] = floor
        search_response = await client.post(
            "/api/v1/search",
            headers=headers,
            json=search_body,
        )
        assert search_response.status_code == 200, search_response.text

        result_ids = {item["id"] for item in search_response.json()["items"]}
        applied_floor = MIN_SEARCH_SIMILARITY if floor is None else floor
        assert memory_id in result_ids, (
            f"the {('pipeline' if use_pipeline else 'legacy')} search path discarded "
            "the README's full-text match under candidate pressure with "
            f"min_similarity={applied_floor:.1f}"
        )

    strict_response = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": query,
            "top_k": top_k,
            "min_similarity": MIN_SEARCH_SIMILARITY,
        },
    )
    assert strict_response.status_code == 200, strict_response.text
    assert memory_id not in {item["id"] for item in strict_response.json()["items"]}, (
        "an explicit similarity floor must remain strict even for a full-text match"
    )
