"""A63 — ``history_query`` lifts the status demotion in scored search.

The contradiction judge marks the older side of an update ``outdated``;
scored search halves that row's score, which on a history question ("what
was…", "did I switch…") buries exactly the value the caller needs. With
``history_query: true`` the demotion is skipped; without it, behaviour is
unchanged.

Vector setup makes the demotion the deciding factor: the outdated target
embeds IDENTICALLY to the query (cosine 1.0), three active fillers sit at
cosine ≈0.93, ``fts_weight`` is 0 so the vector side dominates. Undemoted
the target wins; halved it loses to every filler.
"""

from __future__ import annotations

import hashlib
import math
import uuid

from httpx import AsyncClient

from common.constants import VECTOR_DIM

PREFIX = "/api/v1/storage"

SEARCH_PARAMS = {
    "fts_weight": 0.0,
    "freshness_floor": 1.0,
    "freshness_decay_days": 365,
    "recall_boost_cap": 1.0,
    "recall_decay_window_days": 7,
    "similarity_blend": 1.0,
}


def _unit(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    raw = h * (VECTOR_DIM // len(h) + 1)
    vals = [((raw[i] % 255) - 127) / 128.0 for i in range(VECTOR_DIM)]
    norm = math.sqrt(sum(v * v for v in vals))
    return [v / norm for v in vals]


def _blend(a: list[float], b: list[float], wa: float) -> list[float]:
    wb = math.sqrt(max(0.0, 1 - wa * wa))
    mixed = [wa * x + wb * y for x, y in zip(a, b, strict=True)]
    norm = math.sqrt(sum(v * v for v in mixed))
    return [v / norm for v in mixed]


async def _write(client: AsyncClient, tenant_id: str, fleet_id: str, content: str, emb: list[float]) -> str:
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": "a63-history-test",
            "memory_type": "fact",
            "content": content,
            "embedding": emb,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "weight": 0.7,
            "visibility": "scope_team",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _search(client: AsyncClient, tenant_id: str, emb: list[float], history_query: bool) -> list[str]:
    body = {
        "tenant_id": tenant_id,
        "embedding": emb,
        "query": "zqxv nonlexical probe",  # matches nothing lexically
        "top_k": 2,
        "search_params": SEARCH_PARAMS,
    }
    if history_query:
        body["history_query"] = True
    resp = await client.post(f"{PREFIX}/memories/scored-search", json=body)
    assert resp.status_code == 200, resp.text
    return [r["id"] for r in resp.json()]


class TestHistoryQueryDemotionLift:
    async def test_outdated_row_buried_without_flag_surfaced_with_it(
        self, client: AsyncClient, tenant_id: str, fleet_id: str
    ) -> None:
        run = uuid.uuid4().hex[:8]
        target_emb = _unit(f"history-target-{run}")
        noise = _unit(f"noise-{run}")

        target_id = await _write(
            client, tenant_id, fleet_id, f"old backup schedule ran at 01:00 UTC {run}", target_emb
        )
        for i in range(3):
            await _write(
                client,
                tenant_id,
                fleet_id,
                f"filler memory {i} about the backup pipeline {run}",
                _blend(target_emb, noise, 0.93),
            )

        # Mark the target superseded, the way the contradiction judge does.
        patch = await client.patch(
            f"{PREFIX}/memories/{target_id}", json={"tenant_id": tenant_id, "status": "outdated"}
        )
        assert patch.status_code == 200, patch.text

        without_flag = await _search(client, tenant_id, target_emb, history_query=False)
        assert target_id not in without_flag, (
            "outdated row must stay demoted below the fillers on a present-state query"
        )

        with_flag = await _search(client, tenant_id, target_emb, history_query=True)
        assert with_flag and with_flag[0] == target_id, (
            f"history query must surface the superseded row first, got {with_flag}"
        )
