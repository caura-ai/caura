"""oss-0924-m-05 — giving up on an embedding reported success.

Every embed-repair path in the tree funnels into ``_reembed_memory``: the fast
write whose hot-path embed returned ``None``, the atomic-fact and auto-chunk
fan-outs, the update path, and all five of ``_reembed_memories_bulk``'s
per-item fallbacks. Its two terminal exits — the provider still answering
``None`` after ``_REEMBED_MAX_RETRIES``, and the PATCH that loses a vector it
already computed — both logged and then ``return``ed.

``tracked_task`` writes a ``BackgroundTaskLog`` row ONLY when the coroutine it
wraps raises. Returning normally is therefore indistinguishable from success,
so the only table an operator inspects stayed empty while the row stayed
``embedding IS NULL`` — exactly the shape ``record_task_failure`` was split out
for in 09/02 M-40, whose sole adopter until now was
``process_entity_extraction``.

What made it terminal rather than merely quiet: the sweep that would have
repaired the row, ``run_embed_backfill_tick``, registers only when
``embed_backfill_enabled`` is set, that setting defaults FALSE, and its
Pub/Sub topic is Terraform-provisioned and has never been created. A
deployment in that state is how ~430 memories were stranded in the 2026-07-27
incident ``memory_service`` carries a postmortem for, and the reason it took an
incident to notice is these two ``return``s.

What these tests do NOT claim: the row does not repair anything, and it does
not make the MEMORY self-describing. ``metadata.embedding_pending`` still reads
``True`` whether the embed is coming or gone for good, and nothing here changes
that.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services import memory_service, task_tracker
from tests._scoped_module import scoped
from tests.conftest import close_scheduled_coro

pytestmark = pytest.mark.asyncio

TENANT_ID = f"test-m05-{uuid.uuid4().hex[:8]}"


async def _noop_sleep(_secs: float) -> None:
    return None


def _storage(*, update_raises: BaseException | None = None) -> MagicMock:
    """A storage client whose row is live and still unembedded."""
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": "m1",
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": None,
        }
    )
    sc.update_embedding = AsyncMock(side_effect=update_raises)
    return sc


def _reembed_env(stack: ExitStack, *, embed_returns, storage: MagicMock) -> None:
    """Enter the patch stack every case below shares.

    ``deployment_mode="inline"`` deliberately: it is the default
    (``core_api.config.Settings``), it is the branch
    ``_schedule_embed_or_reembed`` routes to when there is no worker fleet, and
    it is the only branch with no Pub/Sub redelivery behind it — so it is the
    one where a give-up is final.
    """
    for ctx in (
        patch.object(memory_service.settings, "deployment_mode", "inline"),
        patch.object(
            memory_service, "get_embedding", new=AsyncMock(return_value=embed_returns)
        ),
        patch.object(memory_service, "get_storage_client", return_value=storage),
        patch.object(memory_service, "asyncio", scoped(asyncio, sleep=_noop_sleep)),
        patch.object(memory_service, "track_task", side_effect=close_scheduled_coro),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        stack.enter_context(ctx)


# ── the two terminal exits ────────────────────────────────────────────────


async def test_exhausted_retries_are_recorded() -> None:
    """The provider never produced a vector. Nothing raised, so ``tracked_task``
    saw a success; this row is the only durable trace that the memory is in a
    state nothing will repair."""
    memory_id = uuid.uuid4()
    sc = _storage()
    recorder = AsyncMock()

    with ExitStack() as stack:
        _reembed_env(stack, embed_returns=None, storage=sc)
        stack.enter_context(
            patch.object(memory_service, "record_task_failure", new=recorder)
        )
        await memory_service._reembed_memory(memory_id, "hello", TENANT_ID)

    recorder.assert_awaited_once()
    task_name, recorded_id, recorded_tenant, exc = recorder.await_args.args
    assert task_name == memory_service._REEMBED_STRANDED_TASK
    assert recorded_id == memory_id
    assert recorded_tenant == TENANT_ID
    assert str(memory_service._REEMBED_MAX_RETRIES) in str(exc)
    # The row was never touched, which is what makes the record the only signal.
    sc.update_embedding.assert_not_awaited()


async def test_a_computed_vector_lost_at_the_patch_is_recorded() -> None:
    """The costlier of the two: the provider call succeeded and the result was
    dropped on the way to the row. ``_reembed_memories_bulk`` reschedules this
    case; the per-item path cannot — the retry would be this same coroutine, so
    a storage fault that persists makes it a loop — which leaves saying so."""
    memory_id = uuid.uuid4()
    sc = _storage(update_raises=RuntimeError("storage refused the PATCH"))
    recorder = AsyncMock()

    with ExitStack() as stack:
        _reembed_env(stack, embed_returns=[0.1] * VECTOR_DIM, storage=sc)
        stack.enter_context(
            patch.object(memory_service, "record_task_failure", new=recorder)
        )
        await memory_service._reembed_memory(memory_id, "hello", TENANT_ID)

    recorder.assert_awaited_once()
    task_name, recorded_id, _tenant, exc = recorder.await_args.args
    assert task_name == memory_service._REEMBED_STRANDED_TASK
    assert recorded_id == memory_id
    # The message, not the type, is what separates this exit from the one
    # above — both land under the same ``task_name`` on purpose, because the
    # consequence an operator queries for is identical.
    assert "not persisted" in str(exc)


async def test_a_successful_reembed_records_nothing() -> None:
    """The counterweight. ``background_task_log`` is a failure table; a writer
    that also fired on success would make every count taken from it
    meaningless."""
    sc = _storage()
    recorder = AsyncMock()

    with ExitStack() as stack:
        _reembed_env(stack, embed_returns=[0.1] * VECTOR_DIM, storage=sc)
        stack.enter_context(
            patch.object(memory_service, "record_task_failure", new=recorder)
        )
        stack.enter_context(
            patch.object(
                memory_service,
                "tracked_task",
                new=MagicMock(side_effect=close_scheduled_coro),
            )
        )
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)

    sc.update_embedding.assert_awaited_once()
    recorder.assert_not_awaited()


# ── the contract the recording must not cost ──────────────────────────────


async def test_recording_does_not_break_fire_and_forget() -> None:
    """``_reembed_memory`` repairs a row that was committed and ACKed to the
    caller long ago. If a failure to record the failure could escape, this fix
    would have converted a stranded row into a reported background failure with
    a stack trace — louder, and still stranded. ``record_task_failure`` swallows
    its own storage errors; this pins it through the real helper rather than
    trusting the docstring."""
    with ExitStack() as stack:
        _reembed_env(stack, embed_returns=None, storage=_storage())
        stack.enter_context(
            patch.object(
                task_tracker,
                "get_storage_client",
                side_effect=RuntimeError("storage down"),
            )
        )
        # No exception escapes.
        await memory_service._reembed_memory(uuid.uuid4(), "hello", TENANT_ID)


async def test_the_row_reaches_storage_with_the_memory_id() -> None:
    """End to end through the real ``record_task_failure``: what the row buys is
    that it names the memory to re-embed by hand. A row without ``memory_id``
    would be a counter, not a work list."""
    memory_id = uuid.uuid4()
    log_sc = MagicMock()
    log_sc.add_task_failure = AsyncMock()

    with ExitStack() as stack:
        _reembed_env(stack, embed_returns=None, storage=_storage())
        stack.enter_context(
            patch.object(task_tracker, "get_storage_client", return_value=log_sc)
        )
        await memory_service._reembed_memory(memory_id, "hello", TENANT_ID)

    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == memory_service._REEMBED_STRANDED_TASK
    assert row["memory_id"] == str(memory_id)
    assert row["tenant_id"] == TENANT_ID
    assert row["status"] == "failed"
    # Not the literal "NoneType: None" that ``traceback.format_exc()`` yields
    # outside an ``except`` — nothing raised on this exit, and a fabricated
    # traceback would send the reader looking for a crash that never happened.
    assert "NoneType: None" not in row["error_traceback"]
