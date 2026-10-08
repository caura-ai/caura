"""The write path does what it records, and records what it did (B33).

The write-pipeline group of the 2026-10-01 audit's search and query-cost batch:

* L-116: a strong write's semantic gate skipped, with no trace, when no
  embedding could be computed.
* L-17: ``near_duplicate_merged`` was stamped before the merge ran, and stayed
  true when the merge failed or lost its link to a contradiction verdict.
* L-19: the merge set the new row's status to ``active``, over the status its
  writer chose.
* L-182: an extract-only preview paid for an embedding nothing reads.
* L-183: the auto-chunk branch found an exact duplicate only after paying for
  enrichment and chunking.
* L-184: the bulk re-embed read its rows one GET at a time, on the reader, and
  PATCHed them one at a time.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.constants import VECTOR_DIM
from core_api.pipeline.compositions import write as compositions
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write import schedule_background_tasks as background
from core_api.pipeline.steps.write.check_exact_duplicate import CheckExactDuplicate
from core_api.pipeline.steps.write.check_semantic_duplicate import (
    CheckSemanticDuplicate,
)
from core_api.pipeline.steps.write.detect_near_duplicate import DetectNearDuplicate
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.schemas import MemoryCreate
from core_api.services import memory_service
from tests.conftest import close_scheduled_coro

pytestmark = pytest.mark.unit

_VEC = [0.1] * VECTOR_DIM
NEW = str(uuid.uuid4())
OLD = str(uuid.uuid4())
SUBJECT = uuid.uuid4()


def _create(content: str = "the release train is shipped", **fields) -> MemoryCreate:
    return MemoryCreate(
        tenant_id="t", fleet_id="f1", agent_id="a1", content=content, **fields
    )


class _Storage:
    """What the merge asked storage to do, and the answers it gets."""

    def __init__(self, *, linked=True, retire_fails: bool = False) -> None:
        self.linked = linked
        self.retire_fails = retire_fails
        self.calls: list[tuple] = []
        self.patches: list[tuple[str, dict]] = []

    async def set_supersedes_if_null(self, memory_id, tenant_id, *, supersedes_id):
        self.calls.append(("link", memory_id, supersedes_id))
        if isinstance(self.linked, Exception):
            raise self.linked
        return self.linked

    async def update_memory_status(self, memory_id, status, supersedes_id=None, **_kw):
        self.calls.append(("status", memory_id, status, supersedes_id))
        if self.retire_fails:
            raise RuntimeError("storage refused the retire")
        return {"ok": True}

    async def update_memory(self, memory_id, tenant_id, data):
        self.patches.append((memory_id, data["metadata_patch"]))
        return {"ok": True}


async def _merge(sc: _Storage) -> None:
    with patch.object(background, "get_storage_client", return_value=sc):
        await background._merge_near_duplicate(NEW, OLD, "t")


# ── L-116: a gate with no vector says it did not run ──────────────────────


async def test_l116_a_gate_with_no_vector_says_it_did_not_run():
    metadata: dict = {}
    ctx = PipelineContext(
        data={
            "input": _create(),
            "embedding": None,
            "memory_fields": {"metadata": metadata},
        },
        tenant_config=SimpleNamespace(semantic_dedup_enabled=True),
    )

    await CheckSemanticDuplicate().execute(ctx)

    assert metadata.get("dedup_skipped_reason") == "embedding_unavailable"
    assert metadata["_system"]["dedup_skipped_reason"] == "embedding_unavailable"


async def test_l116_a_tenant_without_semantic_dedup_records_nothing():
    """The tenant turned the gate off, so nothing was waived. Passes on main too:
    the guard against marking every write of such a tenant."""
    metadata: dict = {}
    ctx = PipelineContext(
        data={
            "input": _create(),
            "embedding": None,
            "memory_fields": {"metadata": metadata},
        },
        tenant_config=SimpleNamespace(semantic_dedup_enabled=False),
    )

    await CheckSemanticDuplicate().execute(ctx)

    assert metadata == {}


# ── L-17: merged means merged ─────────────────────────────────────────────


async def test_l17_the_detector_records_a_pending_decision_not_a_merge():
    metadata: dict = {}
    candidate = {
        "id": OLD,
        "similarity": 0.9,
        "status": "active",
        "subject_entity_id": str(SUBJECT),
        "predicate": "status",
        "object_value": "in progress",
    }
    ctx = PipelineContext(
        data={
            "input": _create(
                subject_entity_id=SUBJECT, predicate="status", object_value="shipped"
            ),
            "embedding": _VEC,
            "memory_fields": {"metadata": metadata},
        },
        tenant_config=SimpleNamespace(
            semantic_dedup_enabled=True, merge_near_duplicates=True
        ),
    )

    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=candidate),
    ):
        await DetectNearDuplicate().execute(ctx)

    assert "near_duplicate_merged" not in metadata
    assert ctx.data["merge_supersedes_id"] == OLD
    assert metadata["_system"]["near_duplicate_merge_pending"] is True


async def test_l17_a_merge_that_lands_says_so_and_clears_the_decision():
    sc = _Storage()

    await _merge(sc)

    assert [memory_id for memory_id, _ in sc.patches] == [NEW]
    recorded = sc.patches[0][1]["_system"]
    assert recorded["near_duplicate_merged"] is True
    assert recorded["near_duplicate_merge_pending"] is None


@pytest.mark.parametrize(
    ("sc", "reason", "asked_to_retire"),
    [
        (_Storage(linked=False), "not_linked", False),
        (_Storage(linked=RuntimeError("storage down")), "link_failed", False),
        (_Storage(retire_fails=True), "candidate_not_retired", True),
    ],
    ids=["link lost to another writer", "link failed", "retire failed"],
)
async def test_l17_a_merge_that_does_not_land_says_why(sc, reason, asked_to_retire):
    """A lost link is a contradiction verdict that claimed the row first, or a
    row gone since: the candidate must then stay current."""
    await _merge(sc)

    assert [memory_id for memory_id, _ in sc.patches] == [NEW]
    recorded = sc.patches[0][1]["_system"]
    assert recorded["near_dup_merge_skipped"] == reason
    assert recorded["near_duplicate_merge_pending"] is None
    assert "near_duplicate_merged" not in recorded
    assert (("status", OLD, "outdated", None) in sc.calls) is asked_to_retire


async def _background(sc: _Storage, status: str) -> dict:
    memory = {
        "id": NEW,
        "status": status,
        "metadata_": {
            "near_duplicate_of": OLD,
            "near_duplicate_merge_pending": True,
            "_system": {"near_duplicate_merge_pending": True},
        },
    }
    ctx = PipelineContext(
        data={
            "input": _create(),
            "memory": memory,
            "embedding": _VEC,
            "resolved_write_mode": "fast",
            "merge_supersedes_id": OLD,
        },
        tenant_config=SimpleNamespace(
            enrichment_enabled=False,
            enrichment_provider="none",
            entity_extraction_enabled=False,
        ),
    )
    with (
        patch.object(background, "get_storage_client", return_value=sc),
        patch.object(background, "track_task", new=MagicMock()),
        patch.object(
            background,
            "tracked_task",
            new=MagicMock(side_effect=close_scheduled_coro),
        ),
    ):
        await background.ScheduleBackgroundTasks().execute(ctx)
    return memory


async def test_l17_the_response_shows_the_merge_it_made():
    memory = await _background(_Storage(), "active")

    assert memory["metadata_"].get("near_duplicate_merged") is True
    assert memory["metadata_"]["_system"]["near_duplicate_merged"] is True
    assert memory["metadata_"]["near_duplicate_merge_pending"] is None


async def test_l17_a_held_write_leaves_its_merge_to_the_release():
    """Nothing a held write schedules reaches another memory before a person
    releases it, and the release replays the pending decision."""
    sc = _Storage()

    memory = await _background(sc, QUARANTINED_MEMORY_STATUS)

    assert sc.calls == []
    assert memory["metadata_"]["near_duplicate_merge_pending"] is True


# ── L-19: the link writes the link ────────────────────────────────────────


async def test_l19_the_merge_never_writes_the_new_rows_status():
    sc = _Storage()

    await _merge(sc)

    assert ("link", NEW, OLD) in sc.calls
    assert [call for call in sc.calls if call[1] == NEW] == [("link", NEW, OLD)]
    assert ("status", OLD, "outdated", None) in sc.calls


# ── L-182: a preview needs no vector ──────────────────────────────────────


async def test_l182_a_preview_pays_for_no_embedding():
    ctx = PipelineContext(
        data={
            "input": _create(persist=False),
            "content_hash": None,
            "cached_embedding": None,
        },
        tenant_config=SimpleNamespace(
            enrichment_enabled=False, enrichment_provider="none"
        ),
    )

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=_VEC),
        ) as embed,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_not_called()
    assert ctx.data["embedding"] is None


# ── L-183: a duplicate long document is refused before it costs anything ──


def test_l183_the_auto_chunk_branch_checks_for_an_exact_duplicate_first():
    """``build_enrichment_pipeline`` is what the auto-chunk branch runs before
    chunking; the gate must follow the hash it reads, as on the inline paths."""
    names = [step.name for step in compositions.build_enrichment_pipeline()._steps]

    assert "check_exact_duplicate" in names, names
    gate = names.index("check_exact_duplicate")
    assert gate == names.index("compute_content_hash") + 1, names
    assert gate < names.index("parallel_embed_enrich"), names


@pytest.mark.parametrize(
    "builder",
    [compositions.build_persist_pipeline, compositions.build_fast_persist_pipeline],
)
def test_l183_the_fall_through_does_not_look_the_hash_up_again(builder):
    """The fall-through runs on the context the enrichment pipeline checked, so
    a second lookup of the same hash is a storage round trip that can only
    agree. A duplicate that lands in between is the insert's 409."""
    names = [step.name for step in builder()._steps]

    assert "check_exact_duplicate" not in names, names


async def test_l183_a_preview_has_no_hash_to_check():
    """The same pipeline serves extract-only, which stores nothing."""
    sc = MagicMock()
    sc.find_by_content_hash = AsyncMock(return_value={"id": OLD})
    ctx = PipelineContext(data={"input": _create(persist=False), "content_hash": None})

    with patch(
        "core_api.pipeline.steps.write.check_exact_duplicate.get_storage_client",
        return_value=sc,
    ):
        await CheckExactDuplicate().execute(ctx)

    sc.find_by_content_hash.assert_not_called()


# ── L-184: the bulk re-embed reads once and writes side by side ───────────


class _BulkStorage:
    def __init__(self) -> None:
        self.bulk_reads: list[tuple[list[str], bool]] = []
        self.single_reads = 0
        self.in_flight = 0
        self.most_in_flight = 0
        self.patched: list[str] = []

    @staticmethod
    def _row(memory_id: str) -> dict:
        return {
            "id": memory_id,
            "fleet_id": "f1",
            "embedding": None,
            "deleted_at": None,
        }

    async def bulk_get_memories(self, ids, tenant_id, *, read=True):
        self.bulk_reads.append((list(ids), read))
        return [self._row(memory_id) for memory_id in ids]

    async def get_memory(self, memory_id, tenant_id, **_kw):
        self.single_reads += 1
        return self._row(memory_id)

    async def update_embedding(self, memory_id, tenant_id, embedding, **_kw):
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        self.patched.append(memory_id)


async def test_l184_the_batch_is_read_once_on_the_writer_and_patched_side_by_side():
    items = [(uuid.uuid4(), f"bulk body {i}") for i in range(6)]
    ids = [str(memory_id) for memory_id, _ in items]
    sc = _BulkStorage()

    async def _batch(texts, _cfg, **_kw):
        return [_VEC for _ in texts]

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "track_task", side_effect=close_scheduled_coro),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=close_scheduled_coro),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(items, "t", "f1")

    assert sc.bulk_reads == [(ids, False)]
    assert sc.single_reads == 0
    assert sorted(sc.patched) == sorted(ids)
    assert sc.most_in_flight > 1
