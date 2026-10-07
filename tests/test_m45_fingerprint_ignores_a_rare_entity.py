"""M-45: one more trace with a rare entity must not move a cluster's fingerprint.

Forge cut a cluster's entities to the top five by plain id sort before
fingerprinting them, and passed no centralities, so the "top" five were the five
smallest ids. Entity ids are random UUIDs: a new trace mentioning one more entity
whose id sorts low pushed a real one out, the fingerprint changed, and a cluster
an operator had Rejected came back as a new candidate past its poison cooloff.
fingerprint.py promises the opposite (P4): a sixth low-centrality entity does not
change it.
"""

from datetime import UTC, datetime

import pytest

import core_api.services.forge.forge_service as forge_service
from core_api.services.forge.fingerprint import (
    ENTITY_TOP_K,
    ClusterFingerprintInputs,
    compute_fingerprint,
)
from core_api.services.forge.forge_service import ForgeRunResult, run_forge_distill
from tests.test_forge_distill import (
    _capture_writer,
    _golden_llm_response,
    _llm_returns_golden,
    _memory_fetcher_always,
    _poison_never,
    _trace,
)

pytestmark = pytest.mark.unit

# What every trace in the cluster is about; each sorts after "a0".
_SHARED = ["e7", "e8", "e9", "f1", "f2"]


def _cluster(*, late: bool) -> list:
    """Four traces about _SHARED; ``late`` adds one that also names "a0"."""
    traces = [
        _trace(run_id=f"r{i}", agent_id=f"a{i}", entity_ids=_SHARED) for i in range(4)
    ]
    if late:
        traces.append(_trace(run_id="r9", agent_id="a9", entity_ids=[*_SHARED, "a0"]))
    return traces


async def _run(
    monkeypatch: pytest.MonkeyPatch, traces: list, poison_checker=_poison_never
) -> tuple[ForgeRunResult, list]:
    async def build(*_args, **_kwargs):
        return list(traces)

    monkeypatch.setattr(forge_service, "build_session_traces", build)
    captured, writer = _capture_writer()
    result = await run_forge_distill(
        run_label="test-run",
        tenant_id="t1",
        fleet_id="f1",
        window_start=datetime(2026, 5, 1, tzinfo=UTC),
        window_end=datetime(2026, 5, 15, tzinfo=UTC),
        llm_fn=_llm_returns_golden,
        memory_fetcher=_memory_fetcher_always,
        poison_checker=poison_checker,
        candidate_writer=writer,
    )
    return result, captured


async def _fingerprint(monkeypatch: pytest.MonkeyPatch, traces: list) -> str:
    _, captured = await _run(monkeypatch, traces)
    assert len(captured) == 1
    return captured[0]["data"]["cluster_fingerprint"]


async def test_a_rare_entity_with_a_low_id_keeps_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = await _fingerprint(monkeypatch, _cluster(late=False))
    after = await _fingerprint(monkeypatch, _cluster(late=True))

    assert after == before


async def test_a_rejection_under_the_old_selection_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poison row written before the change holds the fingerprint of the K
    smallest ids. It stores nothing else, so it cannot be back-filled; Forge must
    still treat the cluster as rejected rather than propose it again."""
    golden = _golden_llm_response()
    old_selection = sorted({*_SHARED, "a0"})[:ENTITY_TOP_K]
    rejected = compute_fingerprint(
        ClusterFingerprintInputs(
            goal_phrase=golden["goal_phrase"],
            domain=golden["domain"],
            entity_ids=old_selection,
            step_skeleton=golden["step_skeleton"],
        )
    ).fp

    async def poisoned(fp: str) -> bool:
        return fp == rejected

    result, captured = await _run(monkeypatch, _cluster(late=True), poisoned)

    assert captured == []
    assert result.candidates_skipped_poisoned == 1
