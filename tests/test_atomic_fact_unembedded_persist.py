"""Pins the atomic-fact fan-out's persist-unembedded contract.

Both failure exits in the loop used to ``continue`` BEFORE ``create_memory``,
so an embedding failure lost the fact outright. Why persisting beats dropping,
and why the degrade-to-None arm is the one that actually fires in production:
see the fan-out loop comment in ``_enrich_memory_background``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.memory_enrichment import AtomicFact

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-atomic-unembedded"


def _enrichment(**over):
    base = dict(
        memory_type="fact",
        weight=0.5,
        status="active",
        title="t",
        summary="",
        tags=[],
        llm_ms=1,
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
        # The REAL model, not a stub: if AtomicFact grows a required field
        # this test starts failing instead of passing vacuously.
        atomic_facts=[AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row():
    return dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        status="active",
        weight=0.5,
        ts_valid_start=None,
        ts_valid_end=None,
        metadata_={},
        deleted_at=None,
        fleet_id="f1",
        embedding=None,
        content="body",
        visibility="scope_team",
    )


async def _run_fanout(embed_stub):
    """Drive the fan-out with a stubbed ``get_embedding``.

    Returns ``(child_writes, scheduled_reembeds)``.
    """
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_row())
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})

    scheduled: list[tuple] = []

    def _capture_schedule(memory_id, content, tenant_id, **kw):
        # Sync wrapper so the call is recorded at CALL time — the tracked_task
        # stub below closes the coroutine without awaiting it.
        scheduled.append((memory_id, content, tenant_id, kw))

        async def _noop() -> None:
            return None

        return _noop()

    def _stub_tracked_task(coro, *_a, **_k):
        # ``tracked_task`` is async; under a no-op ``track_task`` its body
        # never runs, so close the inner coroutine to avoid a
        # never-awaited warning.
        coro.close()
        return None

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", new=_stub_tracked_task),
        patch.object(memory_service, "_schedule_embed_or_reembed", new=_capture_schedule),
        patch.object(memory_service, "get_embedding", new=embed_stub),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment()),
        ),
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
            uuid.uuid4(), "body", TENANT, "f1", "a"
        )

    children = [c.args[0] for c in sc.create_memory.await_args_list]
    return children, scheduled


async def _degraded(_content, tenant_config=None):
    """``get_embedding``'s documented degrade: return None, do not raise."""
    return None


async def _raises(_content, tenant_config=None):
    raise TimeoutError("embedding gate timeout")


@pytest.mark.parametrize("embed_stub", [_degraded, _raises], ids=["degrades", "raises"])
async def test_failed_embed_still_persists_the_fact(embed_stub) -> None:
    """Neither failure arm may drop the fact.

    Parametrized rather than split: the arms differ only in HOW the embed
    fails, and the contract asserted here — the row exists, unembedded, and
    flagged — is identical for both.
    """
    children, _ = await _run_fanout(embed_stub)

    assert len(children) == 2, (
        f"both atomic facts must be persisted when embedding fails; got {len(children)}"
    )
    assert all(c["embedding"] is None for c in children)
    assert {c["content"] for c in children} == {"alpha fact", "beta fact"}
    # Must be visible to the NULL-embedding sweep: live status, not deleted.
    assert all(c["status"] == "active" for c in children)


@pytest.mark.parametrize("embed_stub", [_degraded, _raises], ids=["degrades", "raises"])
async def test_unembedded_child_is_flagged_embedding_pending(embed_stub) -> None:
    """``embedding_pending`` is public API — agents are told to read it.

    Without it an unembedded child is indistinguishable from a fully-embedded
    row to every consumer.
    """
    children, _ = await _run_fanout(embed_stub)

    # Length first, and deliberately: ``all()`` over an empty sequence is
    # True, so without this the assertion below passes vacuously on exactly
    # the broken code it exists to catch — the pre-fix path wrote no children
    # at all.
    assert len(children) == 2, f"expected 2 child writes to inspect; got {len(children)}"
    assert all(c["metadata_"].get("embedding_pending") is True for c in children), (
        f"unembedded children must carry embedding_pending=True; got "
        f"{[c['metadata_'] for c in children]!r}"
    )


@pytest.mark.parametrize("embed_stub", [_degraded, _raises], ids=["degrades", "raises"])
async def test_unembedded_child_schedules_its_own_reembed(embed_stub) -> None:
    """Durable handoff, not "wait for the nightly sweep".

    ``embed_backfill_enabled`` defaults to False, so a deployment that hasn't
    enabled the sweep would otherwise strand these rows indefinitely.
    """
    children, scheduled = await _run_fanout(embed_stub)

    assert len(scheduled) == len(children) == 2, (
        f"every unembedded child must get a re-embed scheduled; got {len(scheduled)} "
        f"for {len(children)} children"
    )
    assert {s[1] for s in scheduled} == {"alpha fact", "beta fact"}
    assert all(s[2] == TENANT for s in scheduled)
    assert all(s[3].get("is_failure_fallback") is True for s in scheduled), (
        "the provider just failed, so the retry must carry the failure-fallback backoff"
    )

    # The re-embed must target the CHILD row, never the parent. Scheduling
    # against the parent would re-embed content that is not the fact and
    # leave the child unrepaired forever.
    scheduled_ids = {str(s[0]) for s in scheduled}
    parent_ids = {str(m["metadata_"]["parent_memory_id"]) for m in children}
    assert len(parent_ids) == 1, f"all children share one parent; got {parent_ids!r}"
    assert not (scheduled_ids & parent_ids), (
        f"re-embed was scheduled against the PARENT {parent_ids!r}, not the child rows"
    )
    assert len(scheduled_ids) == 2, (
        f"each child needs its own re-embed target; got {scheduled_ids!r}"
    )


async def test_missing_child_id_is_reported_not_counted_as_scheduled() -> None:
    """A persisted child with no usable id must fail LOUD, not be miscounted.

    The summary WARNING says a re-embed was scheduled for each unembedded
    child. If ``create_memory`` returns something without an ``id``, nothing
    can be scheduled — and folding that into the same counter would make the
    one operator-facing signal assert a repair that was never queued.
    """
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_row())
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    # The pathological response: a successful write that yields no id.
    sc.create_memory = AsyncMock(return_value={})

    scheduled: list[tuple] = []

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", new=MagicMock()),
        patch.object(
            memory_service,
            "_schedule_embed_or_reembed",
            new=lambda *a, **k: scheduled.append((a, k)),
        ),
        patch.object(memory_service, "get_embedding", new=_degraded),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment()),
        ),
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
            uuid.uuid4(), "body", TENANT, "f1", "a"
        )

    # The rows were still written — that is the whole point of the fix.
    assert sc.create_memory.await_count == 2
    # But nothing was scheduled, and nothing may claim otherwise.
    assert scheduled == [], f"nothing schedulable without an id; got {scheduled!r}"


async def test_successful_embed_attaches_vector_and_schedules_nothing() -> None:
    """Control: the happy path must be untouched.

    Tolerating a missing embedding must not quietly stop attaching real ones,
    nor schedule redundant re-embeds for rows that already have a vector.
    """
    vec = [0.25] * 8

    async def _ok(_content, tenant_config=None):
        return vec

    children, scheduled = await _run_fanout(_ok)

    assert len(children) == 2
    assert all(c["embedding"] == vec for c in children)
    assert all("embedding_pending" not in c["metadata_"] for c in children), (
        "an embedded child must not be flagged pending"
    )
    assert scheduled == [], f"no re-embed should be scheduled for embedded rows; got {scheduled!r}"
