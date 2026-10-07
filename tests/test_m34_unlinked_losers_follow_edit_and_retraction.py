"""M-34: a winner's edit and Path C's retraction reach the losers detection left
unlinked.

A winner wires its one ``supersedes_id`` to its first loser only, or to none when
it already supersedes a row, so every other loser of the run is known only from
its ``memory_conflicts`` record (caura PR #1815). Two paths followed the edge
alone:

* a content edit of the winner revived the edge's target and left every other
  loser demoted, although each verdict was about the text the edit replaced;
* Path C's retraction re-judged the edge's pair and never the others.

Owner decision 2026-10-07: both now reach the recorded losers, under the rule a
dismissal applies (caura PR #1885). A loser is reverted only when nothing else
holds it: no edge points at it, and no other record that is not dismissed says it
lost to a live, newer row. Retraction re-judges at most 3 of them per run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.schemas import MemoryUpdate
from core_api.services import contradiction_detector, memory_service
from tests._contradiction_batch_compat import install_batch_status_replay_shim
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CONTRADICTED = ("conflicted", "outdated")


def _day(day: int) -> str:
    return f"2026-09-{day:02d}T00:00:00+00:00"


def _mem(
    created_at: str, *, status: str = "active", supersedes_id: str | None = None
) -> dict:
    mid = str(uuid4())
    return {
        "id": mid,
        "tenant_id": "t1",
        "fleet_id": None,
        "agent_id": "a1",
        "content": f"a statement about the harbour, {mid}",
        "memory_type": "fact",
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": supersedes_id,
        "metadata_": {},
        "weight": 0.5,
        "recall_count": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "deleted_at": None,
    }


def _record(new: dict, old: dict, review_status: str = "pending") -> dict:
    return {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "new_memory_id": new["id"],
        "old_memory_id": old["id"],
        "relationship": "exact_value",
        "review_status": review_status,
    }


class _Storage:
    """Rows by id and conflict records. Status writes are applied, and each row
    a write takes from a contradicted status back to ``active`` is listed in
    ``reverted``."""

    def __init__(self, rows, records=(), holders=None):
        self.rows = {r["id"]: r for r in rows}
        self.records = list(records)
        self.holders = holders or {}
        self.reverted: list[str] = []

    async def get_memory(self, memory_id, tenant_id, *, read=True):
        row = self.rows.get(str(memory_id))
        return dict(row) if row else None

    async def update_memory(self, memory_id, tenant_id, patch):
        row = self.rows[str(memory_id)]
        row.update({k: patch[k] for k in ("content", "supersedes_id") if k in patch})
        return dict(row)

    async def update_memory_status(self, memory_id, status, supersedes_id=None, **kw):
        row = self.rows[str(memory_id)]
        if row["status"] in _CONTRADICTED and status == "active":
            self.reverted.append(str(memory_id))
        row["status"] = status
        if kw.get("unset_supersedes"):
            row["supersedes_id"] = None
        return dict(row)

    async def find_by_supersedes_id(self, tenant_id, supersedes_id, *, read=True):
        return list(self.holders.get(str(supersedes_id), []))

    async def list_memory_conflicts(self, tenant_id, *, memory_id=None, **kw):
        return [
            r
            for r in self.records
            if memory_id in (r["new_memory_id"], r["old_memory_id"])
        ]

    async def reset_entity_artifacts(self, tenant_id, memory_id):
        return {"links": 0, "relations": 0, "entities": 0}

    async def find_duplicate_hash(self, tenant_id, content_hash, *args, **kw):
        return None

    async def get_entity_links_for_memories(self, ids, tenant_id):
        return {}


# ---------------------------------------------------------------------------
# A content edit of the winner
# ---------------------------------------------------------------------------


async def _edit(monkeypatch: pytest.MonkeyPatch, storage: _Storage, winner: dict):
    """Replace ``winner``'s content through ``update_memory``."""
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: storage)

    async def _embed(*a, **k):
        return [0.1, 0.2]

    monkeypatch.setattr(memory_service, "get_embedding", _embed)

    class _Config:
        semantic_dedup_enabled = False
        entity_extraction_enabled = False
        enrichment_enabled = False
        enrichment_provider = "none"

    async def _resolve_config(_tenant):
        return _Config()

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(
        memory_service, "track_task", lambda coro, *a, **k: close_scheduled_coro(coro)
    )
    monkeypatch.setattr(memory_service, "tracked_task", lambda coro, *a, **k: coro)
    await memory_service.update_memory(
        UUID(winner["id"]), "t1", MemoryUpdate(content="the harbour moved, corrected")
    )


async def test_editing_a_winner_revives_a_loser_it_left_unlinked(monkeypatch):
    linked = _mem(_day(2), status="conflicted")
    unlinked = _mem(_day(1), status="conflicted")
    winner = _mem(_day(20), supersedes_id=linked["id"])
    storage = _Storage([winner, linked, unlinked], [_record(winner, unlinked)])

    await _edit(monkeypatch, storage, winner)

    assert linked["id"] in storage.reverted, "the edge's loser, revived as before"
    assert unlinked["id"] in storage.reverted, (
        f"the unlinked loser stayed {storage.rows[unlinked['id']]['status']}"
    )


async def test_an_unlinked_loser_another_verdict_holds_stays_demoted(monkeypatch):
    """Control: a live, newer row it lost to in another record still holds it."""
    loser = _mem(_day(1), status="conflicted")
    rival = _mem(_day(10))
    winner = _mem(_day(20))
    storage = _Storage(
        [winner, loser, rival], [_record(winner, loser), _record(rival, loser)]
    )

    await _edit(monkeypatch, storage, winner)

    assert storage.reverted == []


async def test_an_unlinked_loser_an_edge_holds_stays_demoted(monkeypatch):
    """Control: another row's ``supersedes_id`` still points at it."""
    loser = _mem(_day(1), status="conflicted")
    holder = _mem(_day(10), supersedes_id=loser["id"])
    winner = _mem(_day(20))
    storage = _Storage(
        [winner, loser, holder],
        [_record(winner, loser)],
        holders={loser["id"]: [holder]},
    )

    await _edit(monkeypatch, storage, winner)

    assert storage.reverted == []


# ---------------------------------------------------------------------------
# Path C's retraction
# ---------------------------------------------------------------------------


async def _path_c(storage: _Storage, new: dict, verdict: tuple[bool, float]):
    """Run Path C for ``new``; the retraction judge answers ``verdict``."""
    entity_id = str(uuid4())
    entity = {"id": entity_id, "canonical_name": "Northwind", "entity_type": "place"}

    def links(ids, _tenant):
        return {mid: [{"entity_id": entity_id, "role": "subject"}] for mid in ids}

    sc = AsyncMock()
    sc.get_memory = AsyncMock(side_effect=storage.get_memory)
    sc.update_memory_status = AsyncMock(side_effect=storage.update_memory_status)
    sc.find_by_supersedes_id = AsyncMock(side_effect=storage.find_by_supersedes_id)
    sc.list_memory_conflicts = AsyncMock(side_effect=storage.list_memory_conflicts)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.get_entity_links_for_memories = AsyncMock(side_effect=links)
    sc.get_entities_by_ids = AsyncMock(return_value={entity_id: entity})
    install_batch_status_replay_shim(sc)
    judge = AsyncMock(return_value=verdict)
    with (
        patch.object(contradiction_detector, "get_storage_client", return_value=sc),
        patch.object(
            contradiction_detector, "_llm_entity_aware_contradiction_check", judge
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await contradiction_detector.detect_contradictions_by_entities_async(
            UUID(new["id"]), "t1", None
        )
    return judge


async def test_retraction_re_judges_a_loser_detection_left_unlinked():
    linked = _mem(_day(2), status="conflicted")
    unlinked = _mem(_day(1), status="conflicted")
    new = _mem(_day(20), supersedes_id=linked["id"])
    storage = _Storage([new, linked, unlinked], [_record(new, unlinked)])

    await _path_c(storage, new, (False, 0.90))

    assert linked["id"] in storage.reverted, "the edge's verdict, retracted as before"
    assert unlinked["id"] in storage.reverted, (
        f"the unlinked loser stayed {storage.rows[unlinked['id']]['status']}"
    )


async def test_retraction_re_judges_at_most_three_unlinked_losers():
    new = _mem(_day(20))
    losers = [_mem(_day(day), status="conflicted") for day in range(1, 6)]
    storage = _Storage([new, *losers], [_record(new, loser) for loser in losers])

    judge = await _path_c(storage, new, (False, 0.90))

    assert judge.await_count == 3
    assert len(storage.reverted) == 3


async def test_an_unlinked_verdict_the_judge_upholds_stands():
    """Control: the judge confirms the contradiction."""
    loser = _mem(_day(1), status="conflicted")
    new = _mem(_day(20))
    storage = _Storage([new, loser], [_record(new, loser)])

    await _path_c(storage, new, (True, 0.95))

    assert storage.reverted == []


async def test_a_cleared_unlinked_loser_another_verdict_holds_stays_demoted():
    """Control: the judge clears this verdict, but the loser also lost to a live,
    newer row in another record."""
    loser = _mem(_day(1), status="conflicted")
    rival = _mem(_day(10))
    new = _mem(_day(20))
    storage = _Storage(
        [new, loser, rival], [_record(new, loser), _record(rival, loser)]
    )

    await _path_c(storage, new, (False, 0.90))

    assert storage.reverted == []
