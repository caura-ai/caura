"""A deferred-mode auto-chunk parent must be completed by the background path.

`_handle_auto_chunk_from_ctx` has two exits. The 0-1-fact fall-through hands its
context to `build_persist_pipeline`, whose `ScheduleBackgroundTasks` step carries
a branch commented *"Strong mode (or no mode set)"* — this branch's exact case —
that publishes the enrich request and the embed request when either value is
missing. The multi-fact exit builds its rows by hand and never runs that step, so
it scheduled nothing but entity extraction (#856).

Inline deployments never noticed: `ParallelEmbedEnrich` produced both values
synchronously, so there was nothing to backfill. Deferred deployments defer both
— `defer_enrichment = not settings.inline_enrichment` on the arm this branch
takes, since it sets no `resolved_write_mode` — and the parent was left
permanently unembedded and unenriched: no title, no summary, no tags, no
classified type, and `embedding IS NULL`, so unreachable by semantic recall while
its own chunks were fine.

Nothing marked it either. `MemoryOut.metadata` documents an absent
`embedding_pending`/`enrichment_pending` as *"that stage ran inline"*, which for
this row was the opposite of the truth, and `enrichment_pending` is the only flag
core-worker clears.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.enrichment.schema import EnrichmentResult
from core_api.schemas import MemoryCreate
from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig

pytestmark = [pytest.mark.unit]

TENANT = "t-h856"
FLEET = "f1"
AGENT = "a"


def _parent_row() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "fleet_id": FLEET,
        "agent_id": AGENT,
        "memory_type": "fact",
        "title": None,
        "content": "body",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "created_at": datetime(2026, 8, 22, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }


def _config(*, enrichment: bool = True, provider: str = "openai") -> ResolvedConfig:
    return ResolvedConfig(
        {
            "chunking": {"auto_chunk_enabled": True},
            "entity_extraction": {"enabled": False},
            "enrichment": {"enabled": enrichment, "provider": provider},
        }
    )


class _Run:
    def __init__(self) -> None:
        self.parent: dict | None = None
        self.parent_id: str = ""
        self.children: list[dict] = []
        # (label, memory_id) per ``tracked_task`` call — the label is what
        # ``BackgroundTaskLog`` records, so it is also what an operator greps.
        self.tasks: list[tuple[str, str]] = []

    @property
    def labels(self) -> set[str]:
        return {label for label, _ in self.tasks}

    @property
    def meta(self) -> dict:
        return (self.parent or {}).get("metadata_") or {}


async def _run_auto_chunk(
    *,
    deferred: bool,
    config: ResolvedConfig | None = None,
    chunks: tuple[str, ...] = ("chunk one", "chunk two"),
) -> _Run:
    """Drive the multi-fact exit with ``deployment_mode`` set for real.

    ``ctx`` is built the way ``_create_memory_pipeline`` builds it, but
    ``embedding``/``enrichment`` are set to what ``ParallelEmbedEnrich`` actually
    produces in each mode — ``None`` for both when deferred, because this branch
    never sets ``resolved_write_mode`` and so takes the arm keyed on
    ``settings.inline_embedding`` / ``settings.inline_enrichment``.
    """
    run = _Run()
    config = config or _config()

    parent_row = _parent_row()
    run.parent_id = parent_row["id"]
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="a body long enough to be worth chunking " * 60,
    )
    enrichment = None if deferred else EnrichmentResult(llm_ms=5, title="t")
    ctx = SimpleNamespace(
        data={
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": None if deferred else "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "enrichment": enrichment,
            "embedding": None if deferred else [0.0] * 8,
            "t0": 0.0,
        },
        tenant_config=config,
    )

    async def _chunk_content(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in chunks]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] * 8 for _ in texts]

    def _tracked_task(coro, label, memory_id, tenant_id, *a, **k):
        run.tasks.append((label, str(memory_id)))
        # The coroutine is never awaited by these tests; close it so the run
        # does not emit "coroutine was never awaited" for every scheduled task.
        coro.close()
        return MagicMock()

    with (
        patch.object(memory_service.settings, "deployment_mode", "deferred" if deferred else "inline"),
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", _tracked_task),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunk_content),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    if sc.create_memory.await_args_list:
        run.parent = sc.create_memory.await_args_list[0].args[0]
    if sc.create_memories.await_args_list:
        run.children = sc.create_memories.await_args_list[0].args[0]
    return run


# ── the gap ──────────────────────────────────────────────────────────────────


async def test_a_deferred_parent_gets_both_backfills_scheduled() -> None:
    run = await _run_auto_chunk(deferred=True)

    assert run.labels == {"enrich_or_publish", "embed_or_publish"}
    # Both must target the PARENT — the children are already complete, and a
    # backfill pointed at the wrong row repairs nothing.
    assert {mid for _, mid in run.tasks} == {run.parent_id}


async def test_a_deferred_parent_is_marked_pending() -> None:
    """Absent flags claim the stage ran inline. Here neither did."""
    run = await _run_auto_chunk(deferred=True)

    assert run.meta["embedding_pending"] is True
    assert run.meta["enrichment_pending"] is True


def _recorder(seen: dict):
    """Stand in for a scheduler coroutine function, recording at CALL time.

    Deliberately ``def``, not ``async def``. The harness closes each scheduled
    coroutine rather than awaiting it (nothing here should actually publish), so
    a replacement whose body only runs on await never runs at all and leaves
    ``seen`` empty — a test that asserts nothing while looking like it does.
    Returning an inert coroutine keeps the caller's shape identical.
    """

    def _capture(*args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)

        async def _inert():
            return None

        return _inert()

    return _capture


async def test_the_embed_backfill_carries_the_parents_own_content_hash() -> None:
    """The shim dedups on the hash, so it has to be the hash the row was written
    with — this branch computes its own rather than reading ``ctx``."""
    seen: dict = {}

    with patch.object(memory_service, "_schedule_embed_or_reembed", new=_recorder(seen)):
        run = await _run_auto_chunk(deferred=True)

    memory_id, content, _tenant = seen["args"]
    assert str(memory_id) == run.parent_id
    assert seen["content_hash"] == memory_service._content_hash(TENANT, FLEET, content)
    assert run.parent is not None
    assert seen["content_hash"] == run.parent["content_hash"]


async def test_the_enrich_backfill_requests_governance_remediation() -> None:
    """``_schedule_enrich_or_inline`` warns that a third call site must pass this
    unless ``GovernanceDecision`` already applied the verdict. It ran (#852) —
    but only ever with ``enrichment is None`` here, which is its "uncertain
    signal" branch, where it enforces nothing. So the answer is True."""
    seen: dict = {}

    with patch.object(memory_service, "_schedule_enrich_or_inline", new=_recorder(seen)):
        run = await _run_auto_chunk(deferred=True)

    assert seen["run_governance_remediation"] is True
    assert str(seen["args"][0]) == run.parent_id


# ── over-application guards ──────────────────────────────────────────────────


async def test_an_inline_parent_schedules_no_backfill() -> None:
    """Inline already produced both values; scheduling a repair would be a
    wasted provider call and a marker that contradicts the row."""
    run = await _run_auto_chunk(deferred=False)

    assert run.labels == set()
    assert "embedding_pending" not in run.meta
    assert "enrichment_pending" not in run.meta
    assert run.parent is not None and run.parent["embedding"] is not None


async def test_enrichment_disabled_gets_no_enrich_backfill_and_no_marker() -> None:
    """A tenant with enrichment off will never be enriched by anyone, so the
    marker would never clear and the publish would never be consumed. The
    embedding half is independent and still fires."""
    run = await _run_auto_chunk(deferred=True, config=_config(enrichment=False))

    assert run.labels == {"embed_or_publish"}
    assert "enrichment_pending" not in run.meta
    assert run.meta["embedding_pending"] is True


async def test_the_none_provider_gets_no_enrich_backfill_either() -> None:
    run = await _run_auto_chunk(deferred=True, config=_config(provider="none"))

    assert run.labels == {"embed_or_publish"}
    assert "enrichment_pending" not in run.meta


async def test_entity_extraction_still_fires_alongside_the_backfills() -> None:
    """The one thing this branch always scheduled must not have been displaced."""
    config = ResolvedConfig(
        {
            "chunking": {"auto_chunk_enabled": True},
            "entity_extraction": {"enabled": True},
            "enrichment": {"enabled": True, "provider": "openai"},
        }
    )
    run = await _run_auto_chunk(deferred=True, config=config)

    assert run.labels == {"enrich_or_publish", "embed_or_publish", "entity_extraction"}


async def test_the_children_are_still_embedded_inline() -> None:
    """Deliberately unchanged. The children are embedded in one batched call and
    are complete on insert; only the parent was ever the incomplete row."""
    run = await _run_auto_chunk(deferred=True)

    assert len(run.children) == 2
    assert all(c["embedding"] is not None for c in run.children)
    assert not any("embedding_pending" in (c["metadata_"] or {}) for c in run.children)


# ── parity between the function's two exits ──────────────────────────────────


def test_both_exits_of_the_handler_schedule_the_same_backfills() -> None:
    """The drift guard, and the reason #856 existed.

    One exit gets its backfills from ``ScheduleBackgroundTasks``, the other now
    schedules them itself, so nothing in the type system keeps them aligned. This
    asserts the pair by inspection: the labels the multi-fact branch passes to
    ``tracked_task`` must cover every label the step's no-mode branch uses for
    the same two repairs.

    ``entity_extraction`` is shared and included. ``contradiction_detection`` is
    deliberately NOT expected from the auto-chunk branch — it fires there only on
    the embedding-present arm, and auto-chunk parents have never had Path A.
    """
    import ast
    import inspect

    from core_api.pipeline.steps.write import schedule_background_tasks as sbt

    def _labels(source: str) -> set[str]:
        """Second positional argument of every ``tracked_task(...)`` call."""
        found = set()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "tracked_task"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                found.add(node.args[1].value)
        return found

    handler = _labels(inspect.getsource(memory_service._handle_auto_chunk_from_ctx))
    step = _labels(inspect.getsource(sbt.ScheduleBackgroundTasks))

    repairs = {"enrich_or_publish", "embed_or_publish"}
    assert repairs <= step, f"the step stopped scheduling a repair: {sorted(step)}"
    assert repairs <= handler, f"the auto-chunk exit lost a repair: {sorted(handler)}"
    assert "contradiction_detection" not in handler
