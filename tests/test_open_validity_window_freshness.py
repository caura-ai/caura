"""A validity window a caller sets years out does not keep a row fresh.

Scored search gave full freshness to any row whose ``ts_valid_end`` was still
ahead, however old the row. Any writer can set that field, so a window years out
kept a row from ever decaying. A window now counts only while it closes within
the row's decay horizon (a near deadline); one that closes later is neutral, and
the row ages like a row with no window.

End-to-end against Postgres. Three rows share one embedding and miss the query
lexically, so freshness is the only thing that differs. Each is anchored at the
same event, far past its type's decay, through ``freshness_reference`` and
``valid_at``.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

import core_storage_api.services.postgres_service as ps
from common.constants import TYPE_DECAY_DAYS
from common.embedding import fake_embedding
from core_api.clients.storage_client import get_storage_client

_SP = {
    "fts_weight": 0.3,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
    "fts_rank_scale": 6.0,
    "freshness_reference": 1,
}
AS_OF = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
EVENT = AS_OF - timedelta(days=1000)
_SHARED = fake_embedding("one vector so only freshness separates the rows")


async def _insert(tenant_id: str, content: str, *, valid_end: datetime | None) -> str:
    row = await get_storage_client().create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": _SHARED,
            "content_hash": hashlib.sha256(
                f"{tenant_id}:{content}".encode()
            ).hexdigest(),
            "status": "active",
            "visibility": "scope_team",
            "ts_valid_start": EVENT.isoformat(),
            "ts_valid_end": valid_end.isoformat() if valid_end else None,
        }
    )
    return str(row["id"])


async def test_a_far_window_is_neutral_and_a_near_one_still_counts(tenant_id):
    assert TYPE_DECAY_DAYS["fact"] < 1000, "the rows must be past their decay"
    no_window = await _insert(tenant_id, "the gate code is 4411", valid_end=None)
    far = await _insert(
        tenant_id, "the gate code is 9921", valid_end=AS_OF + timedelta(days=3650)
    )
    near = await _insert(
        tenant_id, "the gate code changes soon", valid_end=AS_OF + timedelta(days=10)
    )

    rows = await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=_SHARED,
        query="zzznomatchzzz",
        search_params=dict(_SP),
        top_k=10,
        valid_at=AS_OF,
    )
    freshness = {str(r.Memory.id): float(r.freshness) for r in rows}

    assert set(freshness) == {no_window, far, near}
    assert freshness[no_window] == pytest.approx(0.7)
    assert freshness[far] == pytest.approx(freshness[no_window]), (
        "a window ten years out must not keep a 1000-day-old row at full freshness"
    )
    assert freshness[near] == pytest.approx(1.0)
