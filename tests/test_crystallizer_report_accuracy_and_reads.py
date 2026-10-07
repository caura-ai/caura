"""What a crystallization report says, and what it costs to say it.

L-28: the NEAR_DUPLICATES issue always carried empty ``affected_ids`` and quoted
the 0.95 module constant, even when the tenant's A72 ``dedup_threshold`` had the
sweep run at another value. ``_check_near_duplicates`` returned only the count
and the pairs, so the issue had nothing else to read.

L-180: each run fetched ``/lifecycle-candidates`` three times (once per lifecycle
check) and memory stats and embedding coverage twice each (hygiene, health and
usage), all for identical inputs. A run now reads each once and shares it. Only a
successful read is shared, so a failed one is retried by the next check that needs
it, and each check still fails on its own, as before.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services import crystallizer_service as cs

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ── L-28: the NEAR_DUPLICATES issue ───────────────────────────────────────


async def _near_duplicates(pairs: list[dict], *, threshold=None, tenant_threshold=None):
    sc = AsyncMock()
    sc.check_near_duplicates = AsyncMock(
        side_effect=[
            {"candidate_ids": ["a", "b", "z"], "pairs": pairs},
            {"candidate_ids": [], "pairs": []},
        ]
    )
    sc.mark_dedup_checked = AsyncMock()
    config = (
        SimpleNamespace()
        if tenant_threshold is None
        else SimpleNamespace(crystallizer_dedup_threshold=tenant_threshold)
    )
    with (
        patch.object(cs, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=config),
        ),
    ):
        return await cs._check_near_duplicates("t1", None, threshold=threshold)


def _issue(near_duplicates: dict) -> dict:
    issues = cs._generate_issues({"near_duplicates": near_duplicates}, {}, {})
    return next(i for i in issues if i["code"] == "NEAR_DUPLICATES")


def _pair(a: str, b: str) -> dict:
    return {"id": a, "neighbor_id": b, "similarity": 0.97}


async def test_the_near_duplicates_issue_lists_the_paired_ids():
    dup = await _near_duplicates([_pair("a", "z"), _pair("b", "z")])
    assert _issue(dup)["affected_ids"] == ["a", "z", "b"]


async def test_the_issue_quotes_the_threshold_the_sweep_used():
    dup = await _near_duplicates([_pair("a", "z")], threshold=0.8)
    description = _issue(dup)["description"]
    assert "0.8" in description
    assert "0.95" not in description


async def test_the_tenant_threshold_reaches_the_issue_text():
    dup = await _near_duplicates([_pair("a", "z")], tenant_threshold=0.85)
    assert "0.85" in _issue(dup)["description"]


async def test_near_duplicate_affected_ids_are_capped():
    pairs = [_pair(f"a{i}", f"b{i}") for i in range(cs.MAX_AFFECTED_IDS)]
    dup = await _near_duplicates(pairs)
    assert len(_issue(dup)["affected_ids"]) == cs.MAX_AFFECTED_IDS


# ── L-180: one read per run ───────────────────────────────────────────────


def _storage_for_a_run() -> AsyncMock:
    sc = AsyncMock()
    sc.find_running_report = AsyncMock(return_value=None)
    sc.create_report = AsyncMock(return_value={"id": str(uuid4())})
    sc.update_report = AsyncMock()
    sc.find_orphaned_entities = AsyncMock(return_value=[])
    sc.check_near_duplicates = AsyncMock(
        return_value={"candidate_ids": [], "pairs": []}
    )
    sc.find_broken_entity_links = AsyncMock(return_value=[])
    sc.get_lifecycle_candidates = AsyncMock(
        return_value={
            "expired_still_active": ["e1"],
            "stale_low_weight": ["s1", "s2"],
            "short_content": [],
        }
    )
    sc.get_embedding_coverage = AsyncMock(
        return_value={"total_active": 10, "missing_embeddings": 3, "coverage_pct": 70.0}
    )
    sc.get_memory_stats = AsyncMock(return_value={"total_memories": 10})
    sc.get_entity_coverage = AsyncMock(return_value=5)
    sc.get_type_distribution = AsyncMock(return_value={"fact": 10})
    sc.get_audit_usage = AsyncMock(return_value={})
    return sc


@contextlib.contextmanager
def _run_env(sc):
    with (
        patch.object(cs, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=SimpleNamespace()),
        ),
        patch.object(cs.settings, "type_ii_materializer_shadow", False),
    ):
        yield


async def _report(sc) -> dict:
    with _run_env(sc):
        await cs.run_crystallization("t1", None, auto_crystallize=False)
    return sc.update_report.await_args.args[1]


async def test_lifecycle_candidates_are_read_once_per_run():
    sc = _storage_for_a_run()
    await _report(sc)
    assert sc.get_lifecycle_candidates.await_count == 1


async def test_memory_stats_and_embedding_coverage_are_read_once_per_run():
    sc = _storage_for_a_run()
    await _report(sc)
    assert sc.get_memory_stats.await_count == 1
    assert sc.get_embedding_coverage.await_count == 1


async def test_the_shared_reads_give_the_same_report():
    """Guard: the report's numbers are the ones each section computed before."""
    report = await _report(_storage_for_a_run())
    hygiene = report["hygiene"]
    assert hygiene["expired_still_active"] == {"count": 1, "affected_ids": ["e1"]}
    assert hygiene["stale_memories"] == {"count": 2, "affected_ids": ["s1", "s2"]}
    assert hygiene["short_content"] == {"count": 0, "affected_ids": []}
    assert hygiene["missing_embeddings"] == {"count": 3}
    assert report["health"]["total_memories"] == 10
    assert report["health"]["embedding_coverage_pct"] == 70.0
    assert report["health"]["entity_coverage_pct"] == 50.0
    assert report["usage_data"]["total_memories"] == 10


async def test_a_failed_shared_read_still_fails_each_check_that_needs_it():
    """Guard: only a success is shared, so each check retries and fails alone."""
    sc = _storage_for_a_run()
    sc.get_lifecycle_candidates = AsyncMock(side_effect=RuntimeError("storage down"))
    report = await _report(sc)
    for name in ("expired_still_active", "stale_memories", "short_content"):
        assert report["hygiene"][name] == {"error": True}, name
    assert sc.get_lifecycle_candidates.await_count == 3
