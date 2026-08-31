"""Tenant binding for dedup-review decisions."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


async def _enqueue_review(client: AsyncClient, tenant_id: str) -> dict:
    response = await client.post(
        f"{PREFIX}/memories/dedup-reviews",
        json={
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "review-agent",
            "new_memory_id": None,
            "candidate_memory_id": str(uuid.uuid4()),
            "new_content": "new review content",
            "candidate_content": "candidate review content",
            "similarity": 0.93,
            "judge_verdict": True,
            "judge_confidence": 0.8,
            "decision_band": "judge_band_reject",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _pending_review_ids(client: AsyncClient, tenant_id: str) -> set[str]:
    response = await client.get(
        f"{PREFIX}/memories/dedup-reviews",
        params={"tenant_id": tenant_id},
    )
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


async def test_wrong_tenant_cannot_decide_review(client: AsyncClient) -> None:
    victim = f"test-tenant-dedup-victim-{uuid.uuid4().hex[:8]}"
    attacker = f"test-tenant-dedup-attacker-{uuid.uuid4().hex[:8]}"
    review = await _enqueue_review(client, victim)

    response = await client.post(
        f"{PREFIX}/memories/dedup-reviews/{review['id']}/decision",
        json={"tenant_id": attacker, "status": "dismissed"},
    )

    assert response.status_code == 404, response.text
    assert review["id"] in await _pending_review_ids(client, victim)


async def test_omitted_tenant_cannot_decide_review(client: AsyncClient) -> None:
    tenant_id = f"test-tenant-dedup-omitted-{uuid.uuid4().hex[:8]}"
    review = await _enqueue_review(client, tenant_id)

    response = await client.post(
        f"{PREFIX}/memories/dedup-reviews/{review['id']}/decision",
        json={"status": "dismissed"},
    )

    assert response.status_code == 422, response.text
    assert review["id"] in await _pending_review_ids(client, tenant_id)


async def test_caller_supplied_reviewer_is_rejected(client: AsyncClient) -> None:
    tenant_id = f"test-tenant-dedup-actor-{uuid.uuid4().hex[:8]}"
    review = await _enqueue_review(client, tenant_id)

    response = await client.post(
        f"{PREFIX}/memories/dedup-reviews/{review['id']}/decision",
        json={
            "tenant_id": tenant_id,
            "status": "dismissed",
            "decided_by": "someone-else",
        },
    )

    assert response.status_code == 422, response.text
    assert review["id"] in await _pending_review_ids(client, tenant_id)
