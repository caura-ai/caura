"""The keyword-heuristic enrichment must not mint deprecated types or statuses.

``fake_enrich`` is the fallback for every write with no LLM key (the shipped
default), with the fake provider, and whenever the provider fails. It still
recognises the classifier-deprecated types (commitment / cancellation /
intention) and guesses a lifecycle ``status`` — ``pending`` for task / plan /
commitment, ``confirmed`` for outcome — and core-api persisted both on the
inline, bulk and background paths. ~11 query paths filter ``status='active'``,
so those rows went invisible, and the deprecated types re-entered the corpus the
classifier had folded them out of.
"""

from __future__ import annotations

import pytest

from common.enrichment import enrich_memory
from common.enrichment.constants import CLASSIFIER_DEPRECATED_MEMORY_TYPES
from tests.conftest import get_test_auth, uid

_HEURISTIC_CASES = [
    "Need to review the PR by Friday",  # task → pending
    "The roadmap includes three phases",  # plan → pending
    "We committed to delivering by Q2",  # commitment → pending
    "The project was cancelled last week",  # cancellation
    "We intend to migrate to AWS next quarter",  # intention
    "The migration achieved 99.9% uptime",  # outcome → confirmed
]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("content", _HEURISTIC_CASES)
async def test_heuristic_enrichment_is_active_and_classifiable(monkeypatch, content):
    monkeypatch.setenv("ENTITY_EXTRACTION_PROVIDER", "fake")

    result = await enrich_memory(content)

    assert result.status == "active"
    assert result.memory_type not in CLASSIFIER_DEPRECATED_MEMORY_TYPES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_heuristic_still_classifies_live_types(monkeypatch):
    """Control: only status and the deprecated labels change."""
    monkeypatch.setenv("ENTITY_EXTRACTION_PROVIDER", "fake")

    assert (
        await enrich_memory("Need to review the PR by Friday")
    ).memory_type == "task"
    assert (
        await enrich_memory("We decided to use PostgreSQL")
    ).memory_type == "decision"


@pytest.fixture
def heuristic_enrichment_on(monkeypatch):
    from core_api.config import settings

    monkeypatch.setattr(settings, "use_llm_for_memory_creation", True)
    monkeypatch.setattr(settings, "entity_extraction_provider", "fake")
    monkeypatch.setenv("ENTITY_EXTRACTION_PROVIDER", "fake")


@pytest.mark.asyncio
async def test_single_write_persists_active_status(client, heuristic_enrichment_on):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"heur-single-{uid()}",
        "content": f"Need to review the PR by Friday {uid()}",
        "write_mode": "strong",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["memory_type"] == "task"  # enrichment did run
    assert out["status"] == "active"


@pytest.mark.asyncio
async def test_single_write_does_not_persist_a_deprecated_type(
    client, heuristic_enrichment_on
):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"heur-dep-{uid()}",
        "content": f"We committed to delivering by Q2 {uid()}",
        "write_mode": "strong",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["memory_type"] not in CLASSIFIER_DEPRECATED_MEMORY_TYPES
    assert out["status"] == "active"


@pytest.mark.asyncio
async def test_an_explicit_status_still_wins(client, heuristic_enrichment_on):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"heur-pin-{uid()}",
        "content": f"Need to review the PR by Friday {uid()}",
        "status": "pending",
        "write_mode": "strong",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_bulk_write_persists_active_status(client, sc, heuristic_enrichment_on):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"heur-bulk-{uid()}",
        "items": [
            {"content": f"Need to review the PR by Friday {uid()}"},
            {"content": f"The roadmap includes three phases {uid()}"},
        ],
    }

    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": f"heur-{uid()}"},
    )

    assert resp.status_code == 200, resp.text
    ids = [r["id"] for r in resp.json()["results"] if r.get("id")]
    assert len(ids) == 2
    for mid in ids:
        row = await sc.get_memory(mid, tenant_id=tenant_id)
        assert row["memory_type"] in ("task", "plan")  # enrichment did run
        assert row["status"] == "active", row
