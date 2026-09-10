"""Scored-search response rows must not carry the two large columns nothing reads.

Audit oss-0814-m-39. ``POST /memories/scored-search`` is the hottest read
path — every recall crosses it, overfetched to ``top_k * 2`` rows — and it
serialized each row with the full ``MEMORY_FIELDS``, which includes the
1024-dim ``embedding`` pgvector (~20 KB once ``tolist()``-ed into JSON
floats) and the ``search_vector`` tsvector text. Neither consumer reads
either field: ``ExecuteScoredSearch`` splats them into a namespace no
downstream step touches, and the legacy ``_dict_to_memory_out`` reads a
fixed field list without them. ``MEMORY_LIST_FIELDS`` exists for exactly
this shape of endpoint; these tests pin that scored-search uses it.

The flip side is pinned too: the consumers' actual read-set must stay
present, so this file fails on an over-pruned field list just as it fails
on the two heavy columns coming back.

No database: the service method is stubbed and the request goes through the
real route (auth middleware, body validation, row serialization) via
ASGITransport, the same app surface ``test_storage_auth.py`` exercises.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from common.constants import VECTOR_DIM
from core_storage_api.app import create_app
from core_storage_api.config import settings
from core_storage_api.routers import memories as memories_router
from core_storage_api.schemas import MEMORY_FIELDS, MEMORY_LIST_FIELDS

pytestmark = pytest.mark.unit

_PREFIX = "/api/v1/storage"

# The scoring knobs the route hard-requires (SQL_SCORING_REQUIRED_KEYS);
# values are arbitrary in-range floats — the service call is stubbed.
_SEARCH_PARAMS = {
    "fts_weight": 0.3,
    "freshness_floor": 0.5,
    "freshness_decay_days": 30,
    "recall_boost_cap": 1.5,
    "recall_decay_window_days": 30,
    "similarity_blend": 0.7,
}

_BODY = {
    "tenant_id": "t-row-serialization",
    "embedding": [0.0] * VECTOR_DIM,
    "query": "what does the row carry",
    "top_k": 5,
    "search_params": _SEARCH_PARAMS,
}

# Every Memory field core-api actually reads off a scored-search row —
# the union of ``ExecuteScoredSearch``'s namespace consumers
# (``_memory_to_out`` via LoadAndSerialize, RerankResults, the D12
# diagnostic in PostFilterResults) and the legacy path's
# ``_dict_to_memory_out``. If trimming the serialized field list ever
# cuts into THIS set, the missing field degrades silently on the
# consumer side (every reader is a tolerant ``getattr``/``.get``), so
# this assertion is the loud version.
_CONSUMER_READ_FIELDS = {
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "agent_display_name",
    "memory_type",
    "title",
    "content",
    "weight",
    "source_uri",
    "run_id",
    "metadata_",
    "created_at",
    "expires_at",
    "subject_entity_id",
    "predicate",
    "object_value",
    "ts_valid_start",
    "ts_valid_end",
    "status",
    "visibility",
    "recall_count",
    "last_recalled_at",
    "supersedes_id",
}

# What the route itself attaches beside the Memory columns.
_ROUTE_ADDED_KEYS = {
    "score",
    "similarity",
    "vec_sim",
    "fts_match",
    "has_embedding",
    "status_penalty",
    "fts_score",
    "freshness",
    "entity_boost",
    "recall_boost",
    "temporal_boost",
    "entity_links",
}


def _fake_memory() -> SimpleNamespace:
    """An ORM-shaped row with EVERY ``MEMORY_FIELDS`` attribute populated.

    ``embedding`` and ``search_vector`` are deliberately non-None: the test
    must catch them being *serialized*, not merely observe that a NULL
    column serializes to null.
    """
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=_BODY["tenant_id"],
        fleet_id="f1",
        agent_id="agent-a",
        agent_display_name="Agent A",
        memory_type="fact",
        content="the row under test",
        embedding=[0.001] * VECTOR_DIM,
        weight=0.5,
        source_uri="test://row",
        run_id="run-1",
        metadata_={"k": "v"},
        created_at=now,
        title="row title",
        content_hash="abc123",
        embedded_content_hash="abc123",
        client_request_id=None,
        expires_at=None,
        deleted_at=None,
        search_vector="'row':2 'test':1 'under':3",
        subject_entity_id=None,
        predicate=None,
        object_value=None,
        ts_valid_start=now,
        ts_valid_end=None,
        status="active",
        visibility="scope_team",
        recall_count=3,
        last_recalled_at=now,
        last_dedup_checked_at=None,
        supersedes_id=None,
        confidence=0.9,
        is_inferred=False,
        scope={},
    )


async def _post_scored_search(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    async def _stub(**kwargs) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                Memory=_fake_memory(),
                score=1.2,
                similarity=0.9,
                vec_sim=0.9,
                fts_match=True,
                has_embedding=True,
                status_penalty=1.0,
                fts_score=0.4,
                freshness=0.8,
                entity_boost=1.0,
                recall_boost=1.1,
                temporal_boost=1.0,
                entity_links=[{"entity_id": str(uuid.uuid4()), "role": "subject"}],
            )
        ]

    monkeypatch.setattr(memories_router._svc, "memory_scored_search", _stub)

    app = create_app()
    headers = {"X-Storage-Secret": settings.core_storage_shared_secret.get_secret_value()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"{_PREFIX}/memories/scored-search", json=_BODY, headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    return rows


async def test_rows_do_not_carry_embedding_or_search_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The waste itself: neither heavy column may reach the wire.

    Pre-fix this failed with both keys present — a 1024-float JSON array and
    the tsvector text serialized per row, per request, for consumers that
    drop them on arrival.
    """
    (row,) = await _post_scored_search(monkeypatch)

    assert "embedding" not in row, (
        "scored-search serialized the full embedding vector; no consumer reads it "
        "(has_embedding is the presence signal, and vector-needing callers use GET /memories/{id})"
    )
    assert "search_vector" not in row, (
        "scored-search serialized the FTS tsvector; no consumer reads it "
        "(fts_match/fts_score are the lexical signals)"
    )


async def test_consumers_still_get_every_field_they_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard rail for the trim: the consumer read-set stays intact.

    Every reader on the core-api side is a tolerant ``getattr``/``.get``, so
    losing a field there is a silent degradation (e.g. every result reporting
    ``status=None`` would un-gate the status penalty display). This is the
    assertion that fails loudly instead.
    """
    (row,) = await _post_scored_search(monkeypatch)

    missing = _CONSUMER_READ_FIELDS - set(row)
    assert not missing, f"scored-search rows lost consumer-read fields: {sorted(missing)}"

    # And the full contract, exactly: the serialized Memory columns are
    # MEMORY_LIST_FIELDS (already pinned by the repo as "MEMORY_FIELDS minus
    # the two large columns") plus what the route attaches.
    assert set(row) == set(MEMORY_LIST_FIELDS) | _ROUTE_ADDED_KEYS


def test_single_get_field_list_still_carries_the_vector() -> None:
    """The trim is scoped to scored-search, not to the field catalogue.

    ``consumer.py``'s contradiction deferral reads ``memory.get("embedding")``
    from the single-get response, so ``MEMORY_FIELDS`` (GET /memories/{id},
    load-by-ids, find-successors) must keep both columns.
    """
    assert "embedding" in MEMORY_FIELDS
    assert "search_vector" in MEMORY_FIELDS
    assert "embedding" not in MEMORY_LIST_FIELDS
    assert "search_vector" not in MEMORY_LIST_FIELDS
