"""What a crystallization run does with each cluster it found.

Three ways a found cluster used to be lost:

* selection stopped (``break``) at the first cluster over the 50-memory batch,
  in arbitrary set order, so one large duplicate family starved every cluster
  after it, and a family larger than the batch was never processed at all;
* a cluster whose extracted facts partly FAILED to persist (not 409, which
  means the content already exists) still had every source archived, so the
  failed facts' content left recall with no replacement;
* every swept row was stamped dedup-checked before crystallization decided
  anything, and a stamped row is never a sweep candidate again, so a cluster
  skipped by an LLM outage, a failed fact or the batch cap was never revisited.
"""

from __future__ import annotations

import contextlib
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from core_api.services import crystallizer_service as cs

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_FACT = {"content": "crystallized", "memory_type": "fact", "weight": 0.8}


def _row(mid: UUID) -> dict:
    return {
        "id": str(mid),
        "content": f"content for {mid}",
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
    }


def _chain(ids: list[UUID]) -> list[dict]:
    return [{"id1": str(a), "id2": str(b)} for a, b in pairwise(ids)]


def _storage() -> AsyncMock:
    sc = AsyncMock()

    async def bulk_get(ids, tenant_id):
        return [_row(UUID(i)) for i in ids]

    sc.bulk_get_memories = AsyncMock(side_effect=bulk_get)
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    sc.mark_dedup_checked = AsyncMock(return_value={"ok": True})
    return sc


@contextlib.contextmanager
def _env(sc, *, extracted, create_memory=None, clusters=None):
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(cs, "get_storage_client", return_value=sc))
        stack.enter_context(
            patch(
                "core_api.services.organization_settings.resolve_config",
                AsyncMock(return_value=SimpleNamespace()),
            )
        )
        stack.enter_context(
            patch.object(cs, "_crystallize_cluster", AsyncMock(side_effect=extracted))
        )
        stack.enter_context(
            patch(
                "core_api.services.memory_service.create_memory",
                create_memory
                or AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(id=uuid4())),
            )
        )
        if clusters is not None:
            stack.enter_context(
                patch.object(cs, "_build_clusters", return_value=clusters)
            )
        yield


def _stamped(sc) -> set[str]:
    return {
        mid for call in sc.mark_dedup_checked.await_args_list for mid in call.args[0]
    }


def _crystallized_sources(extracted_mock_calls) -> list[set[str]]:
    return [{m["id"] for m in call.args[0]} for call in extracted_mock_calls]


# ── selection ─────────────────────────────────────────────────────────────


async def test_a_large_family_ahead_of_small_clusters_does_not_starve_them():
    big = [uuid4() for _ in range(60)]
    small_a = [uuid4() for _ in range(3)]
    small_b = [uuid4() for _ in range(3)]
    pairs = _chain(big) + _chain(small_a) + _chain(small_b)
    sc = _storage()
    seen: list[set[str]] = []

    async def extract(memories, _config):
        seen.append({m["id"] for m in memories})
        return [_FACT]

    # The big family FIRST, the order that used to end selection on the spot.
    order = [set(big), set(small_a), set(small_b)]
    with _env(sc, extracted=extract, clusters=order):
        result = await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": pairs}}
        )

    assert {str(m) for m in small_a} in seen
    assert {str(m) for m in small_b} in seen
    assert result["new_memories"] >= 2


async def test_a_family_larger_than_the_batch_is_still_processed():
    big = [uuid4() for _ in range(60)]
    sc = _storage()
    seen: list[set[str]] = []

    async def extract(memories, _config):
        seen.append({m["id"] for m in memories})
        return [_FACT]

    with _env(sc, extracted=extract):
        result = await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(big)}}
        )

    assert len(seen) == 1, "one budget-sized part of the family per run"
    assert len(seen[0]) == 30
    assert result["memories_archived"] == 30
    # The part that did not fit stays unstamped, so the next sweep finds it.
    assert len(_stamped(sc) & {str(m) for m in big}) == 30


# ── archive gate ──────────────────────────────────────────────────────────


async def test_a_failed_fact_keeps_every_source_live():
    ids = [uuid4() for _ in range(3)]
    sc = _storage()
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(id=uuid4()),
            HTTPException(status_code=503, detail="storage unavailable"),
        ]
    )

    async def extract(_memories, _config):
        return [_FACT, dict(_FACT, content="second fact")]

    with _env(sc, extracted=extract, create_memory=create):
        result = await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(ids)}}
        )

    assert result["failed_facts"] == 1
    assert result["memories_archived"] == 0
    sc.batch_update_status.assert_not_awaited()
    assert not (_stamped(sc) & {str(m) for m in ids}), "the cluster must be swept again"


async def test_duplicates_alone_still_license_the_archive():
    """The counterweight: a 409 means the content is already live."""
    ids = [uuid4() for _ in range(3)]
    sc = _storage()
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(id=uuid4()),
            HTTPException(status_code=409, detail="Duplicate memory exists: x"),
        ]
    )

    async def extract(_memories, _config):
        return [_FACT, dict(_FACT, content="second fact")]

    with _env(sc, extracted=extract, create_memory=create):
        result = await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(ids)}}
        )

    assert result["memories_archived"] == 3
    assert {str(m) for m in ids} <= _stamped(sc)


# ── stamping ──────────────────────────────────────────────────────────────


async def test_an_llm_outage_leaves_the_cluster_unstamped():
    ids = [uuid4() for _ in range(3)]
    sc = _storage()

    async def outage(_memories, _config):
        return cs._skip_crystallize()

    with _env(sc, extracted=outage):
        await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(ids)}}
        )

    assert not (_stamped(sc) & {str(m) for m in ids})


async def test_an_empty_answer_from_a_healthy_llm_settles_the_cluster():
    """``[]`` is the model's verdict that nothing is worth keeping, which the
    prompt asks it to give. Treated as an outage, the cluster was re-sent and
    re-paid on every run, and enough of them took the whole batch budget."""
    ids = [uuid4() for _ in range(3)]
    sc = _storage()

    async def nothing_to_keep(_memories, _config):
        return []

    with _env(sc, extracted=nothing_to_keep):
        result = await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(ids)}}
        )

    assert _stamped(sc) == {str(m) for m in ids}
    sc.batch_update_status.assert_not_awaited()
    assert result["memories_archived"] == 0


def _answering(raw):
    """``_crystallize_cluster`` with a healthy provider that answers ``raw``."""
    llm = AsyncMock()
    llm.complete_json = AsyncMock(return_value=raw)

    async def run_call_fn(*_args, call_fn, **_kwargs):
        return await call_fn(llm)

    return patch.object(cs, "call_with_fallback", AsyncMock(side_effect=run_call_fn))


_CONFIG = SimpleNamespace(enrichment_provider="openai")


@pytest.mark.parametrize(
    "raw",
    [{"content": "not a list"}, "not json", [{"memory_type": "fact"}], [None]],
)
async def test_an_unusable_answer_decides_nothing(raw):
    """Not the model saying "nothing to keep": the cluster must come back."""
    with _answering(raw):
        out = await cs._crystallize_cluster([_row(uuid4())], _CONFIG)
    assert out is None


async def test_an_empty_list_is_the_models_verdict():
    with _answering([]):
        out = await cs._crystallize_cluster([_row(uuid4())], _CONFIG)
    assert out == []


async def test_a_crystallized_cluster_is_stamped():
    ids = [uuid4() for _ in range(3)]
    sc = _storage()

    async def extract(_memories, _config):
        return [_FACT]

    with _env(sc, extracted=extract):
        await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain(ids)}}
        )

    assert _stamped(sc) == {str(m) for m in ids}


async def test_a_pair_below_the_cluster_floor_is_stamped():
    """A policy outcome, not a deferral: otherwise every pair is re-swept nightly."""
    a, b = uuid4(), uuid4()
    sc = _storage()
    with _env(sc, extracted=AsyncMock()):
        await cs._run_crystallization(
            "t1", None, {"near_duplicates": {"pairs": _chain([a, b])}}
        )

    assert _stamped(sc) == {str(a), str(b)}


async def test_the_scan_leaves_paired_rows_for_the_crystallizer():
    """``_check_near_duplicates`` stamps the rows with no duplicate only."""
    sc = AsyncMock()
    sc.check_near_duplicates = AsyncMock(
        side_effect=[
            {
                "candidate_ids": ["a", "b", "c"],
                "pairs": [{"id": "a", "neighbor_id": "z", "similarity": 0.97}],
            },
            {"candidate_ids": [], "pairs": []},
        ]
    )
    with patch.object(cs, "get_storage_client", return_value=sc):
        result = await cs._check_near_duplicates("t1", None, threshold=0.95)

    assert result["count"] == 1
    sc.mark_dedup_checked.assert_awaited_once_with(["b", "c"], "t1")
