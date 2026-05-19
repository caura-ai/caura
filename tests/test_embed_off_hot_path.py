"""CAURA-594 — embedding off the memory-write hot path.

Two paths are exercised here:

* **flag=False (SaaS, Step C):** ``ParallelEmbedEnrich`` skips the
  provider call and the deferred backfill is published to
  ``Topics.Memory.EMBED_REQUESTED``. ``_schedule_embed_or_reembed`` is
  the shim that picks the bus.
* **flag=True (OSS standalone, default):** the embed runs inline; on
  inline failure ``_reembed_memory`` retries in-process with a 30s
  backoff so the failing provider gets recovery time.

The original CAURA-594 shortcut tested ``_reembed_memory`` semantics
under both flag values because it was the only backfill path. Step C
keeps those tests (the function is still called from True-mode and from
``_reembed_memories_bulk``'s per-item failure fallbacks), and adds new
cases for the publish path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.schemas import MemoryCreate

pytestmark = pytest.mark.asyncio


TENANT_ID = f"test-594-{uuid.uuid4().hex[:8]}"


def _input(
    content: str = "A test memory long enough to pass any content-length gate.",
) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id="f",
        agent_id="a",
        content=content,
        persist=True,
        entity_links=[],
    )


def _tenant_config(*, enrichment: bool = False) -> SimpleNamespace:
    """Minimal duck-typed tenant config for the step under test."""
    return SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="fake" if enrichment else "none",
    )


def _ctx(*, enrichment: bool = False, cached_embedding=None) -> PipelineContext:
    data: dict = {"input": _input(), "content_hash": "f" * 64}
    if cached_embedding is not None:
        data["cached_embedding"] = cached_embedding
    return PipelineContext(
        db=AsyncMock(), data=data, tenant_config=_tenant_config(enrichment=enrichment)
    )


# ---------------------------------------------------------------------------
# ParallelEmbedEnrich
# ---------------------------------------------------------------------------


async def test_skips_embed_when_flag_off() -> None:
    """F3 Phase 2: ``deployment_mode=deferred`` is the post-Phase-2
    equivalent of legacy ``embed_on_hot_path=False`` for tests against
    ``ParallelEmbedEnrich`` (which now reads the helpers, not the legacy
    flag). The legacy flag still exists in ``Settings`` until Phase 3
    removes it; it stays steering ``schedule_background_tasks.py`` and
    ``memory_service.py`` until those batches migrate too."""
    ctx = _ctx()
    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ) as embed,
    ):
        await ParallelEmbedEnrich().execute(ctx)
    embed.assert_not_called()
    assert ctx.data["embedding"] is None
    assert ctx.data["enrichment"] is None


async def test_runs_embed_when_flag_on() -> None:
    ctx = _ctx()
    fake = [0.1] * VECTOR_DIM
    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=fake),
        ) as embed,
    ):
        await ParallelEmbedEnrich().execute(ctx)
    embed.assert_called_once()
    assert ctx.data["embedding"] == fake


async def test_uses_cached_embedding_even_when_flag_off() -> None:
    """A cached embedding is a pure dict lookup — there's no provider call
    to offload. The step must reuse it regardless of mode."""
    cached = [0.42] * VECTOR_DIM
    ctx = _ctx(cached_embedding=cached)
    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=[0.0] * VECTOR_DIM),
        ) as embed,
    ):
        await ParallelEmbedEnrich().execute(ctx)
    embed.assert_not_called()
    assert ctx.data["embedding"] == cached


# F3 Phase 2 — DROPPED CAPABILITY (asymmetric flag pair)
# ────────────────────────────────────────────────────────────────────
# The original test ``test_enrichment_still_runs_when_flag_off`` here
# pinned a scenario that ONLY exists under asymmetric flags
# ``(embed=False, enrich=True)``: embedding deferred, enrichment
# still inline. Per the Phase 0 audit (and the user's confirmation
# 2026-05-19) no real environment ever runs the asymmetric state;
# every deploy uses canonical ``(F, F)`` or ``(T, T)``.
#
# F3 Phase 2 batch 1 migrated ``parallel_embed_enrich.py`` onto the
# ``deployment_mode``-derived helpers, which co-vary both axes. The
# asymmetric capability is no longer expressible at this layer. The
# old test was deleted intentionally; if you came here looking for
# it via git blame, see the F3 plan in
# memory/fast-vs-strong-investigation.md.


async def test_hint_reembed_skipped_when_flag_off() -> None:
    """Hint re-embed improves retrieval quality via a second embed roundtrip
    — pointless work when we're deferring the first one. The background
    re-embed uses raw content; a follow-up can plumb the hint through."""
    ctx = _ctx(enrichment=True)
    enrichment_result = SimpleNamespace(
        retrieval_hint="a hint that would normally trigger re-embed"
    )

    async def _enrich(*_a, **_k):
        return enrichment_result

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=[0.0] * VECTOR_DIM),
        ) as embed,
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_not_called()
    assert ctx.data["embedding"] is None


# ---------------------------------------------------------------------------
# _reembed_memory
# ---------------------------------------------------------------------------


async def test_reembed_skips_initial_sleep_when_flag_off() -> None:
    """``_reembed_memory`` without ``is_failure_fallback`` must not sleep
    when flag=False. Pre-Step-C this was the deliberate-offload guard;
    post-Step-C the function is no longer called from the deferred path
    (the shim publishes instead) but the contract still holds for any
    code that imports and calls it directly."""
    from core_api.services import memory_service

    slept: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={"id": "m1", "deleted_at": None, "fleet_id": "f"}
    )
    sc.update_embedding = AsyncMock()

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_fake_sleep),
        patch.object(memory_service, "track_task"),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)
    assert slept == [], "flag-off path must not sleep before the first embed attempt"
    sc.update_embedding.assert_awaited_once()


async def test_reembed_sleeps_on_failure_path_when_flag_on() -> None:
    """Flag-on is the provider-failure fallback — keep the 30s backoff
    so the retry doesn't hit the same transient immediately."""
    from core_api.constants import EMBEDDING_REEMBED_DELAY_S
    from core_api.services import memory_service

    slept: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={"id": "m1", "deleted_at": None, "fleet_id": "f"}
    )
    sc.update_embedding = AsyncMock()

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", True),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_fake_sleep),
        patch.object(memory_service, "track_task"),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)
    assert slept and slept[0] == EMBEDDING_REEMBED_DELAY_S


async def test_reembed_schedules_contradiction_after_success() -> None:
    """Coverage-preservation: a deferred item's contradictions would be
    silently skipped unless re-embed schedules them post-success."""
    from core_api.services import memory_service

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": "m1",
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": None,
        }
    )
    sc.update_embedding = AsyncMock()

    # Stubbing ``tracked_task`` lets us read the scheduled task name
    # from its 2nd positional arg and close the inline coroutine to
    # avoid leaked-coroutine warnings at GC time.
    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    async def _noop_sleep(_secs: float) -> None:
        return None

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_noop_sleep),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)

    tracked.assert_called_once()
    assert tracked.call_args.args[1] == "contradiction_detection_post_reembed"


async def test_reembed_race_guard_fires_with_flag_on_too() -> None:
    """The _enrich-wins-the-race scenario exists with flag=True too:
    in fast write mode, a hot-path embed failure causes BOTH
    _reembed and _enrich_memory_background to be scheduled. If the
    guard were gated on flag=False (the earlier bug), _reembed would
    silently overwrite the hint-enhanced embedding with a raw one.
    Regression guard for a review finding."""
    from core_api.services import memory_service

    hint_enhanced = [0.9] * VECTOR_DIM  # written by _enrich_memory_background
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": "m1",
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": hint_enhanced,
        }
    )
    sc.update_embedding = AsyncMock()

    async def _noop_sleep(_secs: float) -> None:
        return None

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        # Flag ON — the configuration where the earlier guard was broken.
        patch.object(memory_service.settings, "embed_on_hot_path", True),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_noop_sleep),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)

    # Critical: hint-enhanced embedding must NOT be overwritten with raw.
    sc.update_embedding.assert_not_called()
    tracked.assert_called_once()
    assert tracked.call_args.args[1] == "contradiction_detection_post_reembed"


async def test_reembed_respects_existing_embedding_from_enrich_race() -> None:
    """_enrich_memory_background may write a hint-enhanced embedding
    before _reembed_memory runs. The re-embed path must NOT overwrite
    the better embedding, but must still fire contradiction detection
    on the existing value so coverage isn't lost."""
    from core_api.services import memory_service

    existing = [0.9] * VECTOR_DIM  # hint-enhanced embedding written by enrich
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": "m1",
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": existing,
        }
    )
    sc.update_embedding = AsyncMock()

    # Directly assert on the detect_contradictions_async args — avoids
    # reaching into a closed-coroutine's frame, which is brittle.
    detect_calls: list[tuple] = []

    def _fake_detect(*args, **_kwargs):
        detect_calls.append(args)

        # Return a no-op coroutine that tracked_task can close cleanly.
        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    async def _noop_sleep(_secs: float) -> None:
        return None

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_noop_sleep),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=_fake_detect,
        ),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)

    sc.update_embedding.assert_not_called()
    tracked.assert_called_once()
    assert tracked.call_args.args[1] == "contradiction_detection_post_reembed"
    # Contradiction detection must fire on the EXISTING (hint-enhanced)
    # embedding, not the raw-content one the re-embed path computed.
    assert len(detect_calls) == 1
    # detect_contradictions_async(memory_id, tenant_id, fleet_id, content, embedding)
    assert detect_calls[0][4] == existing


async def test_bulk_reembed_preserves_batching() -> None:
    """Bulk writes with the flag off must call the provider's batch
    endpoint ONCE for the whole batch — not N times. Regression guard
    against serializing per-item re-embeds after the write."""
    from core_api.services import memory_service

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        side_effect=lambda mid: {"id": mid, "deleted_at": None, "fleet_id": "f"}
    )
    sc.update_embedding = AsyncMock()

    # Record every call into get_embeddings_batch so we can assert that
    # all N items arrive in a single provider roundtrip.
    batch_calls: list[int] = []

    async def _fake_batch(texts, _cfg):
        batch_calls.append(len(texts))
        return [[0.1] * VECTOR_DIM for _ in texts]

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    items = [(uuid.uuid4(), f"memory {i} body") for i in range(5)]

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_fake_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(items, TENANT_ID)

    assert batch_calls == [5], "expected ONE batch call covering all 5 items"
    # update_embedding once per item; contradiction detection scheduled once per item.
    assert sc.update_embedding.await_count == 5
    names = [call.args[1] for call in tracked.call_args_list]
    assert names == ["contradiction_detection_post_reembed"] * 5


async def test_enrich_fires_contradiction_on_existing_embedding_in_saas_mode() -> None:
    """SaaS-mode (embed_on_hot_path=False) contradiction coverage.

    Pre-CAURA-222 this path did a hint re-embed and fired
    ``contradiction_detection_post_enrich`` on the hint-enhanced vector.
    The hint re-embed is gone (caused write/query surface asymmetry —
    see CAURA-222), but SaaS-mode contradiction coverage was preserved
    by adding a dedicated ``contradiction_detection_saas`` branch at
    the bottom of ``_enrich_memory_background``.

    Expected behavior now:
      - No ``update_embedding`` call from this function (worker owns
        the stored vector).
      - Contradiction detection fires once with task name
        ``contradiction_detection_saas`` against the embedding read
        from storage at the top of the function.
    """
    from core_api.services import memory_service

    raw_existing = [0.1] * VECTOR_DIM  # already written by core-worker

    mem_row = {
        "id": "m1",
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": raw_existing,
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
    }
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=mem_row)
    sc.update_embedding = AsyncMock()
    sc.update_memory_status = AsyncMock()
    sc._patch = AsyncMock()

    enrichment = SimpleNamespace(
        memory_type="fact",
        weight=None,
        title=None,
        summary=None,
        tags=None,
        llm_ms=None,
        contains_pii=False,
        pii_types=None,
        retrieval_hint="a stronger retrieval hint",
        ts_valid_start=None,
        ts_valid_end=None,
        status=None,
        atomic_facts=None,
    )

    detect_calls: list[tuple] = []

    def _fake_detect(*args, **_kwargs):
        detect_calls.append(args)

        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=_fake_detect,
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch.object(memory_service, "track_task"),
        # NB: patch the SOURCE (task_tracker.tracked_task) rather than
        # memory_service.tracked_task — _enrich_memory_background does a
        # local ``from core_api.services.task_tracker import tracked_task``
        # that would shadow the module-level patch.
        patch(
            "core_api.services.task_tracker.tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=True,
                    enrichment_provider="fake",
                    entity_extraction_enabled=False,
                )
            ),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "hello", TENANT_ID, "f1", "a"
        )

    # No hint re-embed roundtrip → no update_embedding call from enrich
    # (the worker owns the stored vector under embed_on_hot_path=False).
    sc.update_embedding.assert_not_awaited()
    # SaaS-mode contradiction detection fires on the embedding read
    # from storage at the top of the function.
    names = [call.args[1] for call in tracked.call_args_list]
    assert "contradiction_detection_saas" in names
    assert "contradiction_detection_post_enrich" not in names
    # arg[4] in detect_contradictions_async is the embedding.
    assert any(call[4] == raw_existing for call in detect_calls)


async def test_enrich_no_contradiction_when_no_prior_embedding() -> None:
    """Flow B1: enrich runs before any backfill, overwrites NULL with
    hint-enhanced. Firing contradiction here would duplicate the one
    ``_reembed_memory``'s race-guard fires later (True mode) — skip.

    NOTE (CAURA-594 Step C, False-mode gap): ``core-worker`` does NOT
    fire contradiction detection, so under flag=False this code path
    silently drops contradiction coverage for items where enrich
    happens to win the race against the worker's PATCH. Tracked as a
    follow-up — adding contradiction detection inside the worker (or
    a ``Topics.Memory.EMBEDDED`` consumer back in core-api) closes it.
    """
    from core_api.services import memory_service

    hint_enhanced = [0.9] * VECTOR_DIM
    mem_row = {
        "id": "m1",
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": None,  # _reembed hasn't run yet
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
    }
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=mem_row)
    sc.update_embedding = AsyncMock()
    sc._patch = AsyncMock()

    enrichment = SimpleNamespace(
        memory_type="fact",
        weight=None,
        title=None,
        summary=None,
        tags=None,
        llm_ms=None,
        contains_pii=False,
        pii_types=None,
        retrieval_hint="hint",
        ts_valid_start=None,
        ts_valid_end=None,
        status=None,
        atomic_facts=None,
    )

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(
            memory_service, "get_embedding", new=AsyncMock(return_value=hint_enhanced)
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch.object(memory_service, "track_task"),
        patch(
            "core_api.services.task_tracker.tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=True,
                    enrichment_provider="fake",
                    entity_extraction_enabled=False,
                )
            ),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "hello", TENANT_ID, "f1", "a"
        )

    names = [call.args[1] for call in tracked.call_args_list]
    # No contradiction_detection scheduled from enrich (it'll be fired
    # by _reembed_memory's race guard when it runs later).
    assert "contradiction_detection_post_enrich" not in names
    assert "contradiction_detection" not in names


async def test_reembed_is_failure_fallback_triggers_backoff() -> None:
    """Per-item calls from a failed batch must wait out the 30s backoff
    — otherwise N serial retries hit the still-failing provider with
    zero delay (thundering herd). Guarded by an explicit kwarg so the
    default deliberate-offload path still gets sub-2s freshness."""
    from core_api.constants import EMBEDDING_REEMBED_DELAY_S
    from core_api.services import memory_service

    slept: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": "m1",
            "deleted_at": None,
            "fleet_id": "f",
            "embedding": None,
        }
    )
    sc.update_embedding = AsyncMock()

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch("core_api.services.memory_service.asyncio.sleep", new=_fake_sleep),
        patch.object(memory_service, "track_task"),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memory(
            uuid.uuid4(), "hello", TENANT_ID, is_failure_fallback=True
        )
    assert slept and slept[0] == EMBEDDING_REEMBED_DELAY_S


async def test_bulk_reembed_fallback_passes_is_failure_fallback() -> None:
    """Batch-call failure must fan out to per-item _reembed_memory with
    is_failure_fallback=True, otherwise the per-items skip their sleep
    and thunder onto the failing provider."""
    from core_api.services import memory_service

    called_with: list[dict] = []

    def _capture(*args, **kwargs):
        # Sync wrapper so args are recorded at CALL time (not await time
        # — the tracked_task stub below closes the coroutine without
        # awaiting, so an async-def body would never run).
        called_with.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()

    async def _failing_batch(_texts, _cfg):
        raise RuntimeError("simulated provider outage")

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_failing_batch),
        patch.object(memory_service, "_reembed_memory", new=_capture),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        items = [(uuid.uuid4(), f"m{i}") for i in range(3)]
        await memory_service._reembed_memories_bulk(items, TENANT_ID)

    assert len(called_with) == 3
    for call in called_with:
        assert call["kwargs"].get("is_failure_fallback") is True


async def test_bulk_reembed_fallback_catches_unexpected_exception_types() -> None:
    """The batch-call except clause must be broad enough to catch
    arbitrary provider exception types (auth errors, HTTP client
    errors, connection-pool exhaustion) — otherwise those types
    propagate out and strand all N items permanently unembedded."""
    from core_api.services import memory_service

    class _MockAuthError(Exception):
        """Stands in for e.g. google.auth.exceptions.RefreshError —
        NOT in the original narrow exception list."""

    async def _failing_batch(_texts, _cfg):
        raise _MockAuthError("token refresh failed")

    called: list[dict] = []

    def _capture(*args, **kwargs):
        called.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_failing_batch),
        patch.object(memory_service, "_reembed_memory", new=_capture),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        items = [(uuid.uuid4(), f"m{i}") for i in range(3)]
        await memory_service._reembed_memories_bulk(items, TENANT_ID)

    # All 3 items land in the per-item fallback despite the auth error
    # being outside the original narrow except list.
    assert len(called) == 3
    for call in called:
        assert call["kwargs"].get("is_failure_fallback") is True


async def test_bulk_reembed_reschedules_items_whose_get_memory_failed() -> None:
    """One bad get_memory (return_exceptions=True in the gather) must
    not strand the item permanently unembedded — reschedule it as a
    per-item retry so it eventually lands. The other items in the
    batch should still go through the normal write pass."""
    from core_api.services import memory_service

    mem_a_id = uuid.uuid4()
    mem_b_id = uuid.uuid4()

    async def _get_memory(mid: str):
        if mid == str(mem_a_id):
            raise RuntimeError("storage transient error for item A")
        return {"id": mid, "deleted_at": None, "fleet_id": "f", "embedding": None}

    sc = MagicMock()
    sc.get_memory = AsyncMock(side_effect=_get_memory)
    sc.update_embedding = AsyncMock()

    async def _batch(_texts, _cfg):
        return [[0.1] * VECTOR_DIM, [0.2] * VECTOR_DIM]

    def _fake_detect(*args, **_kwargs):
        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=_fake_detect,
        ),
        patch.object(memory_service, "track_task"),
        # _reembed_memories_bulk uses the module-level tracked_task
        # binding (not a local re-import like _enrich_memory_background),
        # so we patch memory_service.tracked_task directly.
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(
            [(mem_a_id, "body a"), (mem_b_id, "body b")], TENANT_ID
        )

    # Item B went through the normal write pass.
    sc.update_embedding.assert_awaited_once_with(str(mem_b_id), [0.2] * VECTOR_DIM)
    # Item A was rescheduled as a per-item retry (reembed task) AND
    # item B scheduled contradiction detection — both tracked.
    names = [call.args[1] for call in tracked.call_args_list]
    assert "reembed" in names
    assert "contradiction_detection_post_reembed" in names


async def test_bulk_reembed_patch_failure_reschedules_item() -> None:
    """One item's update_embedding raises (e.g. httpx.ConnectError) —
    the loop must (a) catch the broad exception so subsequent items
    still land AND (b) reschedule the failed item as a per-item retry
    so a transient PATCH blip doesn't strand it permanently."""
    from core_api.services import memory_service

    class _MockHTTPError(Exception):
        """Stands in for httpx.ConnectError / aiohttp.ClientError —
        NOT in the original narrow exception list."""

    mem_a_id = uuid.uuid4()
    mem_b_id = uuid.uuid4()

    async def _get_memory(mid: str):
        return {"id": mid, "deleted_at": None, "fleet_id": "f", "embedding": None}

    async def _update_embedding(mid: str, _emb):
        if mid == str(mem_a_id):
            raise _MockHTTPError("connection pool exhausted for item A")

    sc = MagicMock()
    sc.get_memory = AsyncMock(side_effect=_get_memory)
    sc.update_embedding = AsyncMock(side_effect=_update_embedding)

    async def _batch(_texts, _cfg):
        return [[0.1] * VECTOR_DIM, [0.2] * VECTOR_DIM]

    def _fake_detect(*args, **_kwargs):
        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=_fake_detect,
        ),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(
            [(mem_a_id, "body a"), (mem_b_id, "body b")], TENANT_ID
        )

    # Both PATCH calls attempted (loop didn't abort on item A's failure).
    assert sc.update_embedding.await_count == 2
    names = [call.args[1] for call in tracked.call_args_list]
    # Item A failed PATCH → rescheduled as reembed.
    # Item B succeeded → contradiction detection scheduled.
    assert "reembed" in names
    assert "contradiction_detection_post_reembed" in names


async def test_bulk_reembed_respects_existing_embedding_per_item() -> None:
    """Per-item race guard in the bulk path: if enrichment has already
    written a hint-enhanced embedding for item[i], the bulk re-embed
    must NOT overwrite it — only items that are genuinely NULL get
    their fresh batch-computed embedding written."""
    from core_api.services import memory_service

    hint_enhanced = [0.9] * VECTOR_DIM  # already written by _enrich_memory_background
    fresh = [0.1] * VECTOR_DIM  # what the bulk batch call returns

    # mem_a: enrichment already wrote an embedding (race guard should fire)
    # mem_b: still NULL (normal path should fire)
    mem_a_id = uuid.uuid4()
    mem_b_id = uuid.uuid4()

    def _get_memory(mid: str):
        if mid == str(mem_a_id):
            return {
                "id": mid,
                "deleted_at": None,
                "fleet_id": "f",
                "embedding": hint_enhanced,
            }
        return {"id": mid, "deleted_at": None, "fleet_id": "f", "embedding": None}

    sc = MagicMock()
    sc.get_memory = AsyncMock(side_effect=_get_memory)
    sc.update_embedding = AsyncMock()

    async def _batch(_texts, _cfg):
        return [fresh, fresh]

    detect_calls: list[tuple] = []

    def _fake_detect(*args, **_kwargs):
        detect_calls.append(args)

        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=_fake_detect,
        ),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(
            [(mem_a_id, "body a"), (mem_b_id, "body b")], TENANT_ID
        )

    # mem_a: race guard → no PATCH, contradiction on the EXISTING embedding
    # mem_b: normal path → PATCH fresh, contradiction on the fresh embedding
    sc.update_embedding.assert_awaited_once_with(str(mem_b_id), fresh)
    assert len(detect_calls) == 2
    # Order matches the input order; arg[0]=memory_id, arg[4]=embedding
    by_id = {args[0]: args[4] for args in detect_calls}
    assert by_id[mem_a_id] == hint_enhanced
    assert by_id[mem_b_id] == fresh


async def test_bulk_reembed_falls_back_on_length_mismatch() -> None:
    """If the provider returns fewer (or more) embeddings than we asked
    for, strict zip raises — we must fall back to per-item re-embed
    instead of PATCHing some items and leaving the rest silently
    unembedded."""
    from core_api.services import memory_service

    async def _short_batch(texts, _cfg):
        # Provider returned 2 embeddings for 5 inputs — real-world shape
        # when a provider partial-fails mid-batch.
        return [[0.1] * VECTOR_DIM for _ in texts[:2]]

    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={"id": "m", "deleted_at": None, "fleet_id": "f"}
    )
    sc.update_embedding = AsyncMock()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    items = [(uuid.uuid4(), f"m{i}") for i in range(5)]

    with (
        patch.object(memory_service, "get_embeddings_batch", new=_short_batch),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch.object(memory_service, "track_task"),
        patch.object(
            memory_service,
            "tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ) as tracked,
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._reembed_memories_bulk(items, TENANT_ID)

    # No PATCHes landed — we never entered the per-item loop.
    sc.update_embedding.assert_not_called()
    # All 5 items rescheduled as per-item _reembed_memory tasks.
    names = [call.args[1] for call in tracked.call_args_list]
    assert names == ["reembed"] * 5


# ---------------------------------------------------------------------------
# _schedule_embed_or_reembed (CAURA-594 Step C shim)
# ---------------------------------------------------------------------------


async def test_shim_publishes_event_when_flag_off() -> None:
    """SaaS path: defer to ``core-worker`` via the event bus instead of
    spinning up an in-process retry. Verifies the call site collapses
    to a single ``bus.publish`` with a well-formed payload."""
    from common.events import Topics
    from common.events.memory_embed_request import MemoryEmbedRequest
    from core_api.services import memory_service

    published: list[tuple[str, object]] = []

    class _FakeBus:
        async def publish(self, topic, event):  # noqa: ANN001 — duck-typed
            published.append((topic, event))

    fake_bus = _FakeBus()
    memory_id = uuid.uuid4()

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", False),
        patch(
            "common.events.memory_embed_publisher.get_event_bus",
            return_value=fake_bus,
        ),
    ):
        await memory_service._schedule_embed_or_reembed(
            memory_id, "hello", TENANT_ID, content_hash="h" * 64
        )

    assert len(published) == 1
    topic, event = published[0]
    assert topic == Topics.Memory.EMBED_REQUESTED
    assert event.event_type == Topics.Memory.EMBED_REQUESTED
    assert event.tenant_id == TENANT_ID
    # Round-trip the payload through the typed schema — catches drift
    # the dict-key style would miss (e.g. a renamed field still passes
    # the literal string key check but breaks the consumer).
    req = MemoryEmbedRequest.model_validate(event.payload)
    assert req.memory_id == memory_id
    assert req.tenant_id == TENANT_ID
    assert req.content == "hello"
    assert req.content_hash == "h" * 64


async def test_shim_calls_reembed_in_process_when_flag_on() -> None:
    """OSS standalone path (no worker): inline retry. The shim must
    NOT publish; it must call ``_reembed_memory`` so the embed lands
    in the same process."""
    from core_api.services import memory_service

    published: list = []

    class _FakeBus:
        async def publish(self, *_a, **_k):
            published.append(_a)

    captured_args: tuple = ()

    async def _fake_reembed(*args, **kwargs):
        nonlocal captured_args
        captured_args = args

    memory_id = uuid.uuid4()

    with (
        patch.object(memory_service.settings, "embed_on_hot_path", True),
        patch.object(memory_service, "_reembed_memory", new=_fake_reembed),
        patch(
            "common.events.memory_embed_publisher.get_event_bus",
            return_value=_FakeBus(),
        ),
    ):
        await memory_service._schedule_embed_or_reembed(memory_id, "hello", TENANT_ID)

    assert published == [], "OSS path must not publish — no worker to consume"
    assert captured_args == (memory_id, "hello", TENANT_ID)
