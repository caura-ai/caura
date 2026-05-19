"""F3 Phase 0 — pin the ONE branch we intentionally drop in Phase 3.

Background
──────────
F3 replaces the two global flags ``embed_on_hot_path`` and
``enrich_on_hot_path`` with a single ``deployment_mode: Literal[
"inline", "deferred"]`` knob. The deploy YAML proves prod is always
``(False, False)``; OSS local defaults to ``(True, True)``; user
confirmed no environment uses the asymmetric combinations.

The subagent inventory (2026-05-19) identified exactly one production
branch whose existence ONLY makes sense in the asymmetric state:

    core-api/src/core_api/services/memory_service.py:2158
        if embedding is not None and not settings.embed_on_hot_path:
            ... fire Path A contradiction detection ...

This branch lives INSIDE ``_enrich_memory_background`` (the inline-
enrich code path, gated by ``enrich_on_hot_path=True``). It only does
real work when ``embed_on_hot_path=False`` AND the embed-worker has
already PATCHed an embedding into the row by the time enrichment
finishes — i.e. the ``(enrich=inline, embed=deferred)`` race window.

Why this test exists
────────────────────
It is the CANARY for Phase 3's intentional capability loss. Today the
branch fires; Phase 3 deletes both the branch and this test in one
commit, with a code comment marking the deliberate scope reduction.
Pinning the current behavior here means a reviewer can see precisely
what's being dropped and why — not a quiet `git rm`.

The existing characterization coverage for the canonical cells lives
in (not duplicated here):
  - ``tests/test_write_mode_dispatch.py`` (strong + fast × T,T/F,F)
  - ``tests/test_embed_off_hot_path.py``
  - ``tests/test_enrich_off_hot_path.py``
  - ``tests/test_fast_branch_fan_out.py``
  - ``tests/test_consumer_enriched.py``

These all stay green through Phase 1+2+3.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _enrichment_result() -> SimpleNamespace:
    """Minimal EnrichmentResult-shaped object the post-LLM block reads."""
    return SimpleNamespace(
        memory_type=None,
        weight=None,
        title=None,
        summary=None,
        tags=None,
        retrieval_hint=None,
        contains_pii=None,
        pii_types=None,
        ts_valid_start=None,
        ts_valid_end=None,
        status=None,
        llm_ms=None,
        atomic_facts=[],
    )


def _row_with_embedding(embedding: list[float] | None) -> dict:
    """Storage-row shape the in-function code reads after the PATCH."""
    return {
        "id": str(uuid4()),
        "deleted_at": None,
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "metadata_": {},
        "metadata": None,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "embedding": embedding,
    }


async def test_asymmetric_branch_fires_contradiction_when_embed_deferred_and_worker_patched_first() -> (
    None
):
    """The Phase-3-doomed branch at memory_service.py:2158.

    Setup matches the only state where it does real work:
    - ``enrich_on_hot_path=True``  → inline enrich path runs
    - ``embed_on_hot_path=False``  → without this flag asymmetry, the
      branch would never fire (the ``not settings.embed_on_hot_path``
      guard would be False)
    - ``storage_client.get_memory`` returns a row whose ``embedding``
      column is non-None — simulating the embed-worker having already
      PATCHed the row by the time inline enrichment lands here.

    Expectation: ``detect_contradictions_async`` is scheduled via
    ``track_task``. THIS IS THE BEHAVIOR WE INTENTIONALLY LOSE IN
    F3 PHASE 3. The test (and the branch) are deleted together.
    """
    from core_api.services.memory_service import _enrich_memory_background

    memory_id = uuid4()
    embedding = [0.1] * 1536

    fake_tenant_config = SimpleNamespace(
        enrichment_enabled=True,
        entity_extraction_enabled=False,
    )
    fake_sc = MagicMock()
    fake_sc.get_memory = AsyncMock(return_value=_row_with_embedding(embedding))
    fake_sc._patch = AsyncMock()
    fake_sc.update_memory_status = AsyncMock()

    track_task = MagicMock()
    detect_contradictions_async = MagicMock(return_value="coro-sentinel")
    tracked_task = MagicMock(side_effect=lambda coro, label, *_a, **_k: (label, coro))

    with (
        patch(
            "core_api.services.memory_service.settings.embed_on_hot_path",
            False,
        ),
        patch(
            "core_api.services.memory_service.settings.enrich_on_hot_path",
            True,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=fake_tenant_config),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment_result()),
        ),
        patch(
            "core_api.services.memory_service.get_storage_client",
            return_value=fake_sc,
        ),
        patch(
            "core_api.services.memory_service.track_task",
            new=track_task,
        ),
        patch(
            "core_api.services.task_tracker.tracked_task",
            new=tracked_task,
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=detect_contradictions_async,
        ),
    ):
        await _enrich_memory_background(
            memory_id=memory_id,
            content="A test memory for the asymmetric canary.",
            tenant_id="t-f3-canary",
            fleet_id=None,
            agent_id="a",
        )

    # The branch must have fired: detect_contradictions_async was called
    # with the worker-PATCHed embedding, then wrapped in tracked_task
    # under the SaaS-mode label, then handed to track_task.
    detect_contradictions_async.assert_called_once()
    args = detect_contradictions_async.call_args.args
    assert args[0] == memory_id
    assert args[1] == "t-f3-canary"
    assert args[4] == embedding, (
        "branch must pass the embedding it observed on the worker-PATCHed row"
    )

    labels_scheduled = [call.args[0][0] for call in track_task.call_args_list]
    assert "contradiction_detection_saas" in labels_scheduled, (
        f"track_task must receive a tracked_task labelled 'contradiction_detection_saas' "
        f"(the asymmetric-branch label). Got labels: {labels_scheduled}. "
        f"If this assertion fails, the branch at memory_service.py:2158 was "
        f"removed or moved — confirm intent before deleting this canary."
    )


async def test_asymmetric_branch_does_not_fire_when_both_flags_true() -> None:
    """Negative pin: the canonical OSS-inline cell. ``embed_on_hot_path=True``
    means ``not settings.embed_on_hot_path`` is False, so the branch
    short-circuits. ``detect_contradictions_async`` is NOT called from
    this path — the OSS hot path already fired its own Path A via
    ``ScheduleBackgroundTasks``.
    """
    from core_api.services.memory_service import _enrich_memory_background

    memory_id = uuid4()

    fake_tenant_config = SimpleNamespace(
        enrichment_enabled=True,
        entity_extraction_enabled=False,
    )
    fake_sc = MagicMock()
    fake_sc.get_memory = AsyncMock(return_value=_row_with_embedding([0.2] * 1536))
    fake_sc._patch = AsyncMock()
    fake_sc.update_memory_status = AsyncMock()

    track_task = MagicMock()
    detect_contradictions_async = MagicMock(return_value="coro-sentinel")
    tracked_task = MagicMock(side_effect=lambda coro, label, *_a, **_k: (label, coro))

    with (
        patch(
            "core_api.services.memory_service.settings.embed_on_hot_path",
            True,
        ),
        patch(
            "core_api.services.memory_service.settings.enrich_on_hot_path",
            True,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=fake_tenant_config),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment_result()),
        ),
        patch(
            "core_api.services.memory_service.get_storage_client",
            return_value=fake_sc,
        ),
        patch(
            "core_api.services.memory_service.track_task",
            new=track_task,
        ),
        patch(
            "core_api.services.task_tracker.tracked_task",
            new=tracked_task,
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=detect_contradictions_async,
        ),
    ):
        await _enrich_memory_background(
            memory_id=memory_id,
            content="A test memory for the canonical-cell negative pin.",
            tenant_id="t-f3-canary",
            fleet_id=None,
            agent_id="a",
        )

    detect_contradictions_async.assert_not_called()
    labels_scheduled = [call.args[0][0] for call in track_task.call_args_list]
    assert "contradiction_detection_saas" not in labels_scheduled, (
        f"In the canonical (True, True) cell the asymmetric branch must "
        f"NOT fire. The OSS hot path already fired its own Path A. "
        f"Got labels: {labels_scheduled}."
    )
