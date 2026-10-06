"""A judge that abstained because no LLM answered has not reached a verdict.

``call_with_fallback`` does not raise when every provider fails: it returns the
``fake_fn`` result, and in production that is an abstain. The detection entry
points used to treat that as a concluded run, so during a provider outage they
kept the per-memory lock for its full hour (swallowing the back-channel
re-delivery that might have succeeded) and wrote no ``contradiction_stranded``
row, leaving the memory unchecked with no trace. An abstaining run must release
its lock and be recorded exactly like a run that raised.

L-226: a judge switched off on purpose (``contradiction_provider=none``) is not
an outage. ``call_with_fallback`` goes straight to the ``fake_fn`` for it, and
counting that abstain wrote a ``contradiction_stranded`` row on every write
that had a candidate, noise that hid the real outages.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services import contradiction_detector as cd
from core_api.services import task_tracker

pytestmark = pytest.mark.asyncio

TENANT = f"test-abstain-{uuid.uuid4().hex[:8]}"
_VEC = [0.0] * VECTOR_DIM


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "fleet_id": "f1",
        "content": "Alice lives in Boston",
        "status": "active",
        "visibility": "scope_team",
        "deleted_at": None,
    }
    base.update(over)
    return base


async def _providers_all_failed(*, fake_fn, **_kw):
    """``call_with_fallback`` after every provider failed: the fake_fn result."""
    return fake_fn()


def _env(stack: ExitStack, sc: MagicMock, monkeypatch) -> tuple[AsyncMock, MagicMock]:
    # A real provider name, so the fake_fn is the production abstain rather
    # than the heuristic the deliberate ``fake`` provider gets.
    monkeypatch.setattr(cd.settings, "contradiction_provider", "openai")
    stack.enter_context(patch.object(cd, "get_storage_client", lambda: sc))
    stack.enter_context(
        patch.object(cd, "_acquire_content_lock", AsyncMock(return_value=True))
    )
    stack.enter_context(
        patch.object(cd, "_acquire_entity_lock", AsyncMock(return_value=True))
    )
    release = AsyncMock()
    stack.enter_context(patch.object(cd, "_release_lock", release))
    stack.enter_context(
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=SimpleNamespace()),
        )
    )
    stack.enter_context(
        patch.object(
            cd,
            "_rdf_conflict_pass",
            AsyncMock(return_value=cd._RdfPassResult([], [], None, False, [])),
        )
    )
    log_sc = MagicMock()
    log_sc.add_task_failure = AsyncMock()
    stack.enter_context(
        patch.object(task_tracker, "get_storage_client", return_value=log_sc)
    )
    return release, log_sc


async def test_path_a_run_whose_judge_abstained_releases_and_records(
    monkeypatch,
) -> None:
    memory = _row()
    sc = MagicMock()
    sc.find_similar_candidates = AsyncMock(
        return_value=[_row(content="Alice lives in NYC")]
    )
    sc.batch_update_status = AsyncMock()
    with ExitStack() as stack:
        release, log_sc = _env(stack, sc, monkeypatch)
        stack.enter_context(
            patch.object(cd, "call_with_fallback", _providers_all_failed)
        )
        await cd.detect_contradictions_async(
            uuid.UUID(memory["id"]),
            TENANT,
            "f1",
            memory["content"],
            _VEC,
            new_memory=memory,
        )

    release.assert_awaited_once()
    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == cd._CONTRADICTION_STRANDED_TASK
    assert row["memory_id"] == memory["id"]
    assert "abstained" in row["error_message"]
    sc.batch_update_status.assert_not_awaited()


async def test_path_a_batch_abstain_counts_too(monkeypatch) -> None:
    memory = _row()
    sc = MagicMock()
    sc.find_similar_candidates = AsyncMock(
        return_value=[
            _row(content="Alice lives in NYC"),
            _row(content="Alice lives in LA"),
        ]
    )
    with ExitStack() as stack:
        release, log_sc = _env(stack, sc, monkeypatch)
        stack.enter_context(
            patch.object(cd, "call_with_fallback", _providers_all_failed)
        )
        await cd.detect_contradictions_async(
            uuid.UUID(memory["id"]),
            TENANT,
            "f1",
            memory["content"],
            _VEC,
            new_memory=memory,
        )

    release.assert_awaited_once()
    assert (
        "2 verdict(s) abstained"
        in log_sc.add_task_failure.await_args.args[0]["error_message"]
    )


async def test_path_a_run_whose_judge_answered_keeps_its_lock(monkeypatch) -> None:
    """The counterweight: a real "no contradiction" IS a verdict."""
    memory = _row()
    sc = MagicMock()
    sc.find_similar_candidates = AsyncMock(return_value=[_row(content="Bob likes tea")])

    async def _answered(*, call_fn, **_kw):
        return (False, cd._CONF_CLEAN)

    with ExitStack() as stack:
        release, log_sc = _env(stack, sc, monkeypatch)
        stack.enter_context(patch.object(cd, "call_with_fallback", _answered))
        await cd.detect_contradictions_async(
            uuid.UUID(memory["id"]),
            TENANT,
            "f1",
            memory["content"],
            _VEC,
            new_memory=memory,
        )

    release.assert_not_awaited()
    log_sc.add_task_failure.assert_not_awaited()


async def test_path_c_run_whose_judge_abstained_releases_and_records(
    monkeypatch,
) -> None:
    memory = _row()
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])

    async def _retraction_judge_abstains(*_a, **_kw) -> bool:
        # Stands in for the retraction re-judge hitting an outage: its verdict
        # is an abstain, so it declines to retract.
        cd._skip_contradiction_pairwise()
        return False

    with ExitStack() as stack:
        release, log_sc = _env(stack, sc, monkeypatch)
        stack.enter_context(
            patch.object(cd, "_attempt_entity_retraction", _retraction_judge_abstains)
        )
        await cd.detect_contradictions_by_entities_async(
            uuid.UUID(memory["id"]), TENANT, "f1"
        )

    release.assert_awaited_once()
    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["error_message"].startswith("entity detection failed")
    assert "abstained" in row["error_message"]


async def test_an_abstain_outside_a_detection_run_is_harmless() -> None:
    """Other callers of the judge (the in-session API, dedup) have no run to
    report into; the abstain must still just return its verdict."""
    assert cd._skip_contradiction_pairwise() == (False, cd._CONF_FALLBACK)
    assert cd._skip_contradiction_batch(2) == [{}, {}]


# ── a judge switched off on purpose (L-226) ───────────────────────────────


@pytest.mark.parametrize("candidates", [1, 2], ids=["pairwise", "batch"])
async def test_a_judge_switched_off_is_not_an_outage(monkeypatch, candidates) -> None:
    """The real ``call_with_fallback``: for ``none`` it calls no provider."""
    memory = _row()
    sc = MagicMock()
    sc.find_similar_candidates = AsyncMock(
        return_value=[
            _row(content=f"Alice lives in city {i}") for i in range(candidates)
        ]
    )
    sc.batch_update_status = AsyncMock()
    with ExitStack() as stack:
        release, log_sc = _env(stack, sc, monkeypatch)
        monkeypatch.setattr(cd.settings, "contradiction_provider", "none")
        await cd.detect_contradictions_async(
            uuid.UUID(memory["id"]),
            TENANT,
            "f1",
            memory["content"],
            _VEC,
            new_memory=memory,
        )

    release.assert_not_awaited()
    log_sc.add_task_failure.assert_not_awaited()
    sc.batch_update_status.assert_not_awaited()


@pytest.mark.parametrize(("provider", "counted"), [("none", 0), ("openai", 3)])
async def test_only_an_outage_abstain_is_counted(provider, counted) -> None:
    """Every judge call site builds its ``fake_fn`` here, Path C's retraction
    re-judge included. ``openai`` stands for a real provider that failed."""
    holder = cd._JudgeAbstentions()
    token = cd._judge_abstentions.set(holder)
    try:
        pairwise = cd._pairwise_fake_fn(provider, "new", "old")()
        batch = cd._batch_fake_fn(provider, "new", [{}, {}])()
    finally:
        cd._judge_abstentions.reset(token)

    assert pairwise == (False, cd._CONF_FALLBACK)
    assert batch == [{}, {}]
    assert holder.count == counted
