"""Audit batch: the write path's background fan-out and embedding plumbing.

Six findings that all sit on what happens AFTER (or BESIDE) the row is written:
which background work gets scheduled, whether a failure in it is reported, and
whether the embedding backend the plumbing picks is the one the operator
configured.

  * OSS 09/02 M-18 — the fast fan-out ran enrichment for ``provider="none"``.
  * OSS 09/02 L-20 — fast+inline fired entity extraction twice.
  * OSS 08/14 M-06 — a step that RETURNED ``FAILED`` was reported as success.
  * OSS 08/14 M-28 — ``PLATFORM_EMBEDDING_*`` was never bridged to ``os.environ``.
  * OSS 09/02 L-40 — the query-embedding cache key omitted the resolved provider.
  * OSS 08/14 L-34 — entity links went out one HTTP call at a time.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.schedule_background_tasks import (
    ScheduleBackgroundTasks,
)
from core_api.services import memory_service

pytestmark = [pytest.mark.unit]

VECTOR_DIM = 1024
TENANT = "t-fanout"


def _make_input(
    content="A memory with enough content to clear this suite's quality gate.", **kw
):
    from core_api.schemas import MemoryCreate

    return MemoryCreate(
        tenant_id=TENANT, fleet_id="f1", agent_id="a1", content=content, **kw
    )


def _config(**over):
    """A tenant config stub carrying every attribute the fan-out reads."""
    attrs = {
        "enrichment_enabled": True,
        "enrichment_provider": "openai",
        "entity_extraction_enabled": True,
    }
    attrs.update(over)
    return type("C", (), attrs)()


def _fast_ctx(data_input, config, *, embedding=None):
    return PipelineContext(
        data={
            "input": data_input,
            "memory": type("M", (), {"id": uuid.uuid4()})(),
            "embedding": embedding,
            "resolved_write_mode": "fast",
        },
        tenant_config=config,
    )


async def _run_fast_fanout(config, *, embedding=None):
    """Drive the fast branch and report which background tasks it scheduled."""
    scheduled: list[str] = []

    def _track(coro_or_task):
        # ``tracked_task`` is what names the task; capture the label it was
        # given and close the coroutine so nothing leaks into another test.
        return coro_or_task

    def _tracked(coro, label, *a, **kw):
        scheduled.append(label)
        coro.close()
        return MagicMock()

    with (
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.track_task",
            new=MagicMock(side_effect=_track),
        ),
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.tracked_task",
            new=MagicMock(side_effect=_tracked),
        ),
    ):
        await ScheduleBackgroundTasks().execute(
            _fast_ctx(_make_input(), config, embedding=embedding)
        )
    return scheduled


# ── OSS 09/02 M-18 ──────────────────────────────────────────────────────────


async def test_a_provider_of_none_schedules_no_enrichment():
    """``provider="none"`` is not a no-op provider, it is a phantom one.

    ``enrich_memory`` answers it with a bare ``EnrichmentResult()`` whose
    Pydantic defaults are real values — weight 0.7, status "active",
    memory_type fact — and those get PATCHed onto the row. So scheduling
    enrichment for a tenant that switched the provider off does not "do
    nothing": it reweights every fast write 0.5 -> 0.7 and clears the
    ``enrichment_pending`` marker as though an LLM had read the content.
    """
    scheduled = await _run_fast_fanout(_config(enrichment_provider="none"))
    assert "background_enrichment" not in scheduled, (
        "enrichment was scheduled for provider='none' — the phantom default "
        "EnrichmentResult will be written to the row"
    )


async def test_a_real_provider_still_schedules_enrichment():
    """The guard must not turn enrichment off for everyone else."""
    scheduled = await _run_fast_fanout(_config())
    assert "background_enrichment" in scheduled


# ── OSS 09/02 L-20 ──────────────────────────────────────────────────────────


async def test_entity_extraction_is_scheduled_exactly_once():
    """One extraction per write, from the one place that always runs.

    ``ScheduleBackgroundTasks`` fires extraction, and ``_enrich_memory_background``
    used to fire it again on the fast+inline path — two LLM passes over the same
    content. The surviving trigger is this one deliberately: the other sat past
    an ``enrich_memory`` call whose failure returns early, so a failed enrichment
    silently cost the row its extraction as well.
    """
    scheduled = await _run_fast_fanout(_config())
    assert scheduled.count("entity_extraction") == 1, scheduled


def test_the_background_enrichment_helper_no_longer_fires_extraction():
    """The other half of L-20, pinned at the source.

    A counting test on the live fan-out cannot see this one: the duplicate fired
    from inside a background task that the test's own ``track_task`` stub never
    runs. So this reads the function instead — the same shape of guard the repo
    already uses for "exactly one implementation" invariants.
    """
    import inspect

    src = inspect.getsource(memory_service._enrich_memory_background)
    assert "process_entity_extraction(" not in src, (
        "_enrich_memory_background fires entity extraction again; "
        "ScheduleBackgroundTasks already scheduled it for every write that "
        "can reach this function"
    )


# ── OSS 08/14 M-06 ──────────────────────────────────────────────────────────


async def test_a_returned_failed_marks_the_pipeline_failed():
    """``result.failed`` must mean "a step failed", not "a step raised"."""
    from core_api.pipeline.runner import Pipeline
    from core_api.pipeline.step import StepOutcome, StepResult

    class _Failing:
        @property
        def name(self) -> str:
            return "failing"

        async def execute(self, ctx):
            return StepResult(StepOutcome.FAILED, detail={"error": "storage said no"})

    result = await Pipeline("t", [_Failing()]).run(PipelineContext(data={}))
    assert result.failed, "a returned FAILED was reported as a successful pipeline"


async def test_a_returned_failed_does_not_stop_later_steps():
    """Returning FAILED reports a failure; raising aborts. Two different statements.

    The nightly entity-linking pipeline is why: when only the merge stage fails,
    cross-link discovery and relation inference are still worth running.
    """
    from core_api.pipeline.runner import Pipeline
    from core_api.pipeline.step import StepOutcome, StepResult

    ran: list[str] = []

    class _Failing:
        @property
        def name(self) -> str:
            return "failing"

        async def execute(self, ctx):
            ran.append("failing")
            return StepResult(StepOutcome.FAILED)

    class _After:
        @property
        def name(self) -> str:
            return "after"

        async def execute(self, ctx):
            ran.append("after")
            return None

    result = await Pipeline("t", [_Failing(), _After()]).run(PipelineContext(data={}))
    assert ran == ["failing", "after"], ran
    assert result.failed


async def test_a_failed_entity_linking_run_is_not_reported_as_success():
    """The symptom M-06 was filed for.

    ``entity_link`` discarded the ``PipelineResult`` entirely, and
    ``Pipeline.run`` does not re-raise — it catches a step's exception INTO
    ``result.failed``. So a nightly run whose steps all failed recorded SUCCESS
    with ``links_created=0``, which reads exactly like a healthy run of an org
    with nothing left to link.
    """
    from core_api.pipeline.step import StepOutcome, StepResult
    from core_api.services.lifecycle_audit import _CoreApiLifecycleAdapter

    failed = MagicMock()
    failed.failed = True
    failed.steps = [
        StepResult(StepOutcome.FAILED, detail={"error": "all clusters failed"})
    ]

    svc = _CoreApiLifecycleAdapter(MagicMock())
    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = True

    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value=failed)

    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=cfg),
        ),
        patch(
            "core_api.pipeline.compositions.entity_linking.build_full_entity_linking_pipeline",
            return_value=pipeline,
        ),
        pytest.raises(RuntimeError, match="entity-linking pipeline failed"),
    ):
        await svc.entity_link(org_id="org-1", fleet_id=None)


# ── OSS 08/14 M-28 ──────────────────────────────────────────────────────────


def test_platform_embedding_settings_reach_the_environment(monkeypatch):
    """``common.embedding._platform`` reads ``os.environ``, not ``Settings``.

    pydantic-settings loads ``.env`` into ``Settings`` and never exports it, so
    a deployment configuring platform embeddings the documented way had them
    read back as "" and got no platform embedder — silently, and with the
    expensive failure mode: vectors that persist fine, in the wrong space.
    """
    import os

    from core_api.config import bridge_credentials_to_environ, settings

    monkeypatch.setattr(
        settings, "platform_embedding_provider", "openai", raising=False
    )
    monkeypatch.setattr(
        settings, "platform_embedding_model", "text-embedding-3-small", raising=False
    )

    # Snapshot and restore the WHOLE environment, not only the keys asserted on.
    # ``bridge_credentials_to_environ`` writes every credential it has a setting
    # for — ``ENTITY_EXTRACTION_PROVIDER`` and ``ENTITY_EXTRACTION_MODEL``
    # included — and ``monkeypatch`` restores only what monkeypatch itself set,
    # never what the function under test wrote. Left behind, those make later
    # tests in the same process read a provider they never configured, and the
    # failure lands on whichever file happens to run next.
    before = dict(os.environ)
    try:
        for name in (
            "PLATFORM_EMBEDDING_PROVIDER",
            "PLATFORM_EMBEDDING_MODEL",
            "PLATFORM_EMBEDDING_API_KEY",
        ):
            os.environ.pop(name, None)

        bridge_credentials_to_environ()

        assert os.environ.get("PLATFORM_EMBEDDING_PROVIDER") == "openai"
        assert os.environ.get("PLATFORM_EMBEDDING_MODEL") == "text-embedding-3-small"
    finally:
        os.environ.clear()
        os.environ.update(before)


# ── OSS 09/02 L-40 ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_inflight():
    memory_service._inflight_embeddings.clear()
    yield
    memory_service._inflight_embeddings.clear()


async def _key_for(provider, monkeypatch):
    seen: list[str] = []

    async def _capture_get(key):
        seen.append(key)
        return None

    async def _noop_set(key, value, ttl=0):
        return None

    async def _embed(query, tenant_config):
        return [0.1] * 8

    monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", provider)
    with (
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(memory_service, "get_query_embedding", new=_embed),
    ):
        await memory_service._get_or_cache_embedding("same query", "tenant-A", None)
    return seen[0]


async def test_switching_provider_changes_the_query_cache_key(monkeypatch):
    """Different providers embed into different spaces; the key must say which.

    With no tenant ``embedding_model`` set, every other component of the hash is
    invariant across the switch — model falls back to ``OPENAI_EMBEDDING_MODEL``
    regardless of who actually serves the call — so the key was byte-identical
    and stale vectors answered queries for the full cache TTL.
    """
    first = await _key_for("openai", monkeypatch)
    second = await _key_for("voyage", monkeypatch)
    assert first != second, (
        "the same cache key serves two different embedding backends — a query "
        "and its corpus end up in different vector spaces, silently"
    )


# ── OSS 08/14 L-34 ──────────────────────────────────────────────────────────


def test_entity_links_are_chunked_at_the_router_cap():
    """The cap is the router's, and exceeding it loses the whole request.

    ``entity_links`` is unbounded on the caller's side, so a 501-link write must
    not become one 422 that drops every link — the exact failure the per-link
    loop existed to prevent.
    """
    from core_api.pipeline.steps.write import write_memory_row

    assert write_memory_row._LINK_BULK_CHUNK == 500


async def test_many_links_cost_one_storage_round_trip():
    """Six links, one call — the finding is about N sequential HTTP calls.

    Safe to batch here only because ``entity_bulk_upsert_links`` runs each item
    in its own session precisely so an FK violation on item N cannot roll back
    items 0..N-1 — the same "one bad id must not discard the valid links beside
    it" guarantee the per-link loop was written for. Counting the calls is the
    assertion, so a loop that called the bulk endpoint once per link would still
    fail it.

    Drives the step rather than ``create_memory`` so it needs no database: what
    is under test is how many times the storage client is called, and that is
    visible at the step boundary.
    """
    from core_api.pipeline.steps.write.write_memory_row import WriteMemoryRow
    from core_api.schemas import EntityLinkIn

    links = [EntityLinkIn(entity_id=uuid.uuid4(), role="subject") for _ in range(6)]
    calls: list[list[dict]] = []

    async def _bulk(tenant_id, items):
        calls.append(items)
        return [{**it, "created": True} for it in items]

    sc = MagicMock()
    sc.create_memory = AsyncMock(
        return_value={"id": str(uuid.uuid4()), "created_at": None}
    )
    sc.bulk_upsert_entity_links = _bulk

    ctx = PipelineContext(
        data={
            "input": _make_input(entity_links=links),
            "embedding": [0.1] * VECTOR_DIM,
            "content_hash": uuid.uuid4().hex,
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "summary": "",
                "tags": [],
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
        },
        tenant_config=_config(),
    )

    with (
        patch(
            "core_api.pipeline.steps.write.write_memory_row.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.pipeline.steps.write.write_memory_row.get_hooks",
            return_value=MagicMock(audit_log=None),
        ),
    ):
        await WriteMemoryRow().execute(ctx)

    assert len(calls) == 1, f"{len(links)} links cost {len(calls)} storage round-trips"
    assert len(calls[0]) == len(links)
