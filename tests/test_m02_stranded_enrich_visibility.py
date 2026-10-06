"""oss-0927-m-02 — giving up on enrichment reported success.

Every inline enrichment in the tree funnels into
``_enrich_memory_background``, and each of its terminal exits caught, logged
and returned. ``tracked_task`` writes a ``BackgroundTaskLog`` row ONLY when
the coroutine it wraps raises, so returning normally is indistinguishable from
success: the one table an operator inspects stayed empty while the row kept
``enrichment_pending: true`` and no title, summary or tags.

Strictly worse than the embed-side twin (oss-0924-m-05, #1733), and for one
reason: a memory that loses its embedding has a repair job in principle —
``run_embed_backfill_tick`` is real, gated off by default, and could be
switched on. ``enrichment_pending`` has NO sweep at all. There is no job to
enable and no second chance even in principle; ``tracked_task``'s own comment
says these rows "have no sweep at all and stayed pending forever".

What these tests do NOT claim: the row repairs nothing, and it does not make
the MEMORY self-describing. ``metadata.enrichment_pending`` still reads
``True`` whether enrichment is in flight or gone for good, and its ABSENCE is
still documented as "that stage ran inline". Nothing here changes that.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.enrichment.schema import EnrichmentResult
from core_api.services import memory_service, task_tracker

pytestmark = pytest.mark.asyncio

TENANT = f"test-m02-{uuid.uuid4().hex[:8]}"


def _row(**over):
    """A stored row as ``get_memory`` returns it."""
    base = {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": None,
        "content": "body",
    }
    base.update(over)
    return base


def _config(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enrichment_enabled=enabled,
        enrichment_provider="fake",
        entity_extraction_enabled=False,
        auto_chunk_enabled=False,
        atomic_fact_fanout_enabled=False,
    )


def _enrichment() -> EnrichmentResult:
    """A real ``EnrichmentResult``, not a stand-in.

    The patch-assembly block reads a dozen of its fields; a ``SimpleNamespace``
    shaped by hand passes until someone adds a thirteenth, and then fails as an
    ``AttributeError`` inside the very ``except`` this file is testing — which
    would look exactly like the defect and be a fixture bug.
    """
    return EnrichmentResult(title="t", summary="s", llm_ms=1)


def _env(
    stack: ExitStack,
    *,
    storage: MagicMock,
    config_raises: BaseException | None = None,
    config: SimpleNamespace | None = None,
    enrich_raises: BaseException | None = None,
    enrich_returns=None,
) -> None:
    """Enter the patch stack every case below shares."""
    resolve = (
        AsyncMock(side_effect=config_raises)
        if config_raises is not None
        else AsyncMock(return_value=config if config is not None else _config())
    )
    enrich = (
        AsyncMock(side_effect=enrich_raises)
        if enrich_raises is not None
        else AsyncMock(return_value=enrich_returns)
    )
    for ctx in (
        patch.object(memory_service, "get_storage_client", lambda: storage),
        patch.object(memory_service, "track_task", MagicMock()),
        patch("core_api.services.organization_settings.resolve_config", new=resolve),
        patch("core_api.services.memory_enrichment.enrich_memory", new=enrich),
    ):
        stack.enter_context(ctx)


def _storage(*, update_raises: BaseException | None = None) -> MagicMock:
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_row())
    sc.update_memory = AsyncMock(side_effect=update_raises)
    sc.update_memory_status = AsyncMock(return_value=None)
    return sc


async def _run(stack: ExitStack, memory_id: uuid.UUID):
    return await memory_service._enrich_memory_background(
        memory_id, "body", TENANT, "f1", "a"
    )


# ── the four terminal exits ───────────────────────────────────────────────


async def test_a_failed_config_lookup_is_recorded() -> None:
    """The one exit that fires in ordinary operation. No LLM call was
    attempted, so nothing was spent — but the row is as unenriched as if the
    provider had failed, and nothing will revisit it."""
    memory_id = uuid.uuid4()
    recorder = AsyncMock()
    with ExitStack() as stack:
        _env(stack, storage=_storage(), config_raises=RuntimeError("settings down"))
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, memory_id)

    recorder.assert_awaited_once()
    assert recorder.await_args.args[0] == memory_id
    assert recorder.await_args.args[1] == TENANT
    assert "never ran" in recorder.await_args.args[2]


async def test_a_raising_provider_is_recorded() -> None:
    memory_id = uuid.uuid4()
    recorder = AsyncMock()
    with ExitStack() as stack:
        _env(stack, storage=_storage(), enrich_raises=RuntimeError("provider down"))
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, memory_id)

    recorder.assert_awaited_once()
    assert "provider raised" in recorder.await_args.args[2]


async def test_a_none_from_enrich_memory_is_recorded() -> None:
    """Defensive, and recorded as such. ``enrich_memory`` is annotated
    ``-> EnrichmentResult`` and documents "Never raises; always returns an
    EnrichmentResult", falling back to a keyword heuristic that always
    succeeds. Reaching this branch means that contract broke — which is worth a
    row precisely because nothing else in the tree would notice."""
    memory_id = uuid.uuid4()
    recorder = AsyncMock()
    with ExitStack() as stack:
        _env(stack, storage=_storage(), enrich_returns=None)
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, memory_id)

    recorder.assert_awaited_once()
    assert "contract" in recorder.await_args.args[2]


async def test_an_enrichment_lost_at_the_patch_is_recorded() -> None:
    """The costly one, and the exit the row's description did not name. The LLM
    call SUCCEEDED and this block is what writes the result to the row, so a
    failure here throws away work already paid for."""
    memory_id = uuid.uuid4()
    recorder = AsyncMock()
    sc = _storage(update_raises=RuntimeError("storage refused the PATCH"))
    with ExitStack() as stack:
        _env(stack, storage=sc, enrich_returns=_enrichment())
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, memory_id)

    recorder.assert_awaited_once()
    assert "not persisted" in recorder.await_args.args[2]


# ── the exits that are NOT failures ───────────────────────────────────────


async def test_enrichment_disabled_records_nothing() -> None:
    """The counterweight, and the one that caught over-reach on the embed side.
    ``background_task_log`` is a failure table; a writer that fired on a tenant
    who simply switched enrichment off would make every count from it
    meaningless — and this tenant is the common case, not an edge one."""
    recorder = AsyncMock()
    with ExitStack() as stack:
        _env(stack, storage=_storage(), config=_config(enabled=False))
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, uuid.uuid4())

    recorder.assert_not_awaited()


async def test_a_successful_enrichment_records_nothing() -> None:
    sc = _storage()
    recorder = AsyncMock()
    with ExitStack() as stack:
        _env(stack, storage=sc, enrich_returns=_enrichment())
        stack.enter_context(
            patch.object(memory_service, "_record_enrich_stranded", new=recorder)
        )
        await _run(stack, uuid.uuid4())

    sc.update_memory.assert_awaited()
    recorder.assert_not_awaited()


# ── the contract the recording must not cost ──────────────────────────────


async def test_recording_does_not_break_fire_and_forget() -> None:
    """The memory was committed and ACKed to its writer long ago. If a failure
    to record the failure could escape, this fix would have converted a
    stranded row into a reported background failure with a stack trace —
    louder, and still stranded."""
    with ExitStack() as stack:
        _env(stack, storage=_storage(), enrich_raises=RuntimeError("provider down"))
        stack.enter_context(
            patch.object(
                task_tracker,
                "get_storage_client",
                side_effect=RuntimeError("storage down"),
            )
        )
        # No exception escapes.
        await _run(stack, uuid.uuid4())


async def test_the_row_reaches_storage_with_the_memory_id() -> None:
    """End to end through the real ``record_task_failure``: what the row buys
    is that it names the memory to re-enrich by hand. A row without
    ``memory_id`` would be a counter, not a work list."""
    memory_id = uuid.uuid4()
    log_sc = MagicMock()
    log_sc.add_task_failure = AsyncMock()

    with ExitStack() as stack:
        _env(stack, storage=_storage(), enrich_raises=RuntimeError("provider down"))
        stack.enter_context(
            patch.object(task_tracker, "get_storage_client", return_value=log_sc)
        )
        await _run(stack, memory_id)

    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == memory_service._ENRICH_STRANDED_TASK
    assert row["memory_id"] == str(memory_id)
    assert row["tenant_id"] == TENANT
    assert row["status"] == "failed"


async def test_one_task_name_covers_every_exit() -> None:
    """One predicate for "permanently unenriched", not four. The exits differ
    in ``error_message``, not in the name an operator has to know to query."""
    seen = set()
    for kwargs in (
        {"config_raises": RuntimeError("x")},
        {"enrich_raises": RuntimeError("x")},
        {"enrich_returns": None},
    ):
        log_sc = MagicMock()
        log_sc.add_task_failure = AsyncMock()
        with ExitStack() as stack:
            _env(stack, storage=_storage(), **kwargs)
            stack.enter_context(
                patch.object(task_tracker, "get_storage_client", return_value=log_sc)
            )
            await _run(stack, uuid.uuid4())
        seen.add(log_sc.add_task_failure.await_args.args[0]["task_name"])

    assert seen == {memory_service._ENRICH_STRANDED_TASK}
