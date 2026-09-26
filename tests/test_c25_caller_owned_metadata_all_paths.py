"""C25 — the caller/platform metadata boundary, on every enrichment path.

``set_system_value`` is the boundary: the platform's value always lands in
``metadata["_system"]``, and the legacy top-level key is mirrored only when the
caller does not own it. ``tests/test_c25_system_metadata.py`` pins that
function. This file pins the thing that actually went wrong — **who calls it**.

Enrichment writes ``summary`` / ``tags`` from four places, and C25 was applied
to one of them:

1. ``MergeEnrichmentFields`` — the synchronous single write. Correct since C25.
2. ``create_memories_bulk`` — wrote ``metadata["summary"]`` directly, so the
   boundary held for a write and not for the same payload sent as a batch of one.
3. ``_enrich_memory_background`` — the inline-deployment async task. Wrote the
   LLM's summary straight over the caller's, seconds after the write that set it.
4. ``core-worker``'s deferred ``_build_patch`` — same, plus it had nothing to
   decide with: ``agent_provided_fields`` covers ORM columns only, so the
   caller's metadata keys never reached the worker at all. Pinned in
   ``core-worker/tests/test_consumer_enrich.py``, a separate pytest root.

The shape of the bug is why it survived: a caller's ``summary`` and a
platform-written one are the same key, so no path could tell them apart by
inspection. The key set has to be captured at write time and carried.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.system_metadata import SYSTEM_NAMESPACE

# ``unit`` only at module level: the three helper tests are sync, and a
# blanket asyncio mark warns on each. Async tests carry their own.
pytestmark = pytest.mark.unit

TENANT = "t-c25-paths"


@contextlib.contextmanager
def _enrichment_actually_runs():
    """Force the two gates that decide whether enrichment runs at all.

    Both are needed and CI is why. ``enrichment_enabled`` falls back to a
    global that a fresh test tenant resolves to False; ``enrichment_provider``
    resolves to ``"none"`` in CI (see the note in ``organization_settings.py``),
    and ``create_memories_bulk`` gates on ``!= "none"`` at both the inline merge
    and the deferred publish. Overriding only the first passes locally and
    silently skips the whole code path in CI.

    A proxy over the REAL resolved config rather than a stand-in, so every other
    attribute — governance flags, embedding policy, the many the bulk path reads
    — stays authentic.
    """
    from core_api.services import organization_settings

    class _On:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def enrichment_enabled(self):
            return True

        @property
        def enrichment_provider(self):
            return "fake"

    real_resolve = organization_settings.resolve_config

    async def _resolve(tenant_id):
        return _On(await real_resolve(tenant_id))

    with patch.object(organization_settings, "resolve_config", new=_resolve):
        yield


def _enrichment(**over):
    base = {
        "memory_type": "decision",
        "weight": 0.9,
        "status": "active",
        "title": "Enriched title",
        "summary": "PLATFORM SUMMARY",
        "tags": ["platform-tag"],
        "llm_ms": 12,
        "contains_pii": False,
        "pii_types": [],
        "retrieval_hint": "",
        "business_relevance": "business",
        "ts_valid_start": None,
        "ts_valid_end": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# The helper that captures the key set at write time.
# ---------------------------------------------------------------------------


def test_helper_reports_only_caller_ownable_keys():
    """Narrowed to ``CALLER_OWNABLE_KEYS``. ``set_system_value`` consults the
    set for no other key, so anything else would be inert — and the deferred
    path puts this on the wire, where a caller's arbitrary metadata key NAMES
    are worth not carrying."""
    data = SimpleNamespace(
        metadata={"summary": "mine", "project": "apollo", "tags": ["x"]}
    )
    assert memory_service._caller_owned_enrichment_metadata_keys(data) == [
        "summary",
        "tags",
    ]


def test_helper_returns_none_when_the_caller_owns_nothing():
    """``None`` rather than ``[]``, matching the sibling
    ``_agent_provided_enrichment_fields``'s "trust enrichment for everything"
    convention — the two are read the same way downstream."""
    data = SimpleNamespace(metadata={"project": "apollo"})
    assert memory_service._caller_owned_enrichment_metadata_keys(data) is None


def test_helper_tolerates_absent_or_non_dict_metadata():
    """Synthetic inputs and callers that pass no metadata at all must not raise
    — this runs on the write hot path."""
    assert (
        memory_service._caller_owned_enrichment_metadata_keys(SimpleNamespace()) is None
    )
    assert (
        memory_service._caller_owned_enrichment_metadata_keys(
            SimpleNamespace(metadata=None)
        )
        is None
    )
    assert memory_service._caller_owned_enrichment_metadata_keys(object()) is None


# ---------------------------------------------------------------------------
# Path 3 — the inline-deployment async task.
# ---------------------------------------------------------------------------


async def _run_background(caller_owned, *, row_metadata):
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(
        return_value={
            "id": str(uuid.uuid4()),
            "memory_type": "fact",
            "status": "active",
            "weight": 0.5,
            "ts_valid_start": None,
            "ts_valid_end": None,
            "metadata_": row_metadata,
            "deleted_at": None,
            "fleet_id": "f1",
            "embedding": None,
            "content": "body",
        }
    )
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
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
            uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a",
            caller_owned_metadata_keys=caller_owned,
        )

    # Same guard as the bulk test: if the task returned early the assertions
    # would read an empty dict and fail with an opaque KeyError instead of
    # saying the path never ran.
    assert sc.update_memory.await_count == 1, (
        f"the enrichment patch never happened ({sc.update_memory.await_count} "
        "calls) — the task returned before the metadata merge"
    )
    applied: dict = {}
    for call in sc.update_memory.await_args_list:
        for arg in list(call.args) + list(call.kwargs.values()):
            if isinstance(arg, dict):
                applied.update(arg)
    return applied.get("metadata_", {})


@pytest.mark.asyncio
async def test_inline_task_leaves_a_caller_owned_summary_alone():
    """The clobber, on the path that runs on every inline deployment."""
    meta = await _run_background(["summary"], row_metadata={"summary": "MINE"})
    assert meta["summary"] == "MINE"
    assert meta[SYSTEM_NAMESPACE]["summary"] == "PLATFORM SUMMARY"


@pytest.mark.asyncio
async def test_inline_task_still_fills_a_summary_the_caller_did_not_set():
    """The boundary must not become "never write summary" — when the caller
    owns nothing, the legacy mirror is still how a C25-unaware reader sees it."""
    meta = await _run_background(None, row_metadata={})
    assert meta["summary"] == "PLATFORM SUMMARY"
    assert SYSTEM_NAMESPACE in meta, (
        "the platform namespace was never written — this path populated only "
        "the legacy keys before C25 reached it"
    )
    assert meta[SYSTEM_NAMESPACE]["summary"] == "PLATFORM SUMMARY"


@pytest.mark.asyncio
async def test_inline_task_owns_keys_independently():
    """Owning ``summary`` must not pin ``tags`` too."""
    meta = await _run_background(["summary"], row_metadata={"summary": "MINE"})
    assert meta["summary"] == "MINE"
    assert meta["tags"] == ["platform-tag"]


@pytest.mark.asyncio
async def test_inline_task_leaves_caller_owned_tags_alone():
    """``tags`` is the other half of ``CALLER_OWNABLE_KEYS`` and needs its own
    case: every call site wires ``caller_keys`` per key, so a fix that threaded
    it into the ``summary`` call and forgot the ``tags`` call two lines below
    would ship green on a summary-only test."""
    meta = await _run_background(
        ["summary", "tags"], row_metadata={"summary": "MINE", "tags": ["mine"]}
    )
    assert meta["tags"] == ["mine"]
    assert meta[SYSTEM_NAMESPACE]["tags"] == ["platform-tag"]


# ---------------------------------------------------------------------------
# Forwarding — the gate is useless if the key set never arrives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_forwards_the_keys_to_the_inline_path():
    captured: dict = {}

    async def _capture(*a, **kw):
        captured.update(kw)
        return None

    with (
        patch.object(memory_service, "_enrich_memory_background", new=_capture),
        patch.object(memory_service.settings, "deployment_mode", "inline"),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a",
            SimpleNamespace(enrichment_enabled=True),
            caller_owned_metadata_keys=["summary"],
        )

    assert captured.get("caller_owned_metadata_keys") == ["summary"]


@pytest.mark.asyncio
async def test_schedule_forwards_the_keys_to_the_deferred_path():
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        return None

    with (
        patch.object(memory_service, "publish_memory_enrich_request", new=_capture),
        patch.object(memory_service.settings, "deployment_mode", "deferred"),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a",
            SimpleNamespace(enrichment_enabled=True),
            caller_owned_metadata_keys=["summary"],
        )

    assert captured.get("caller_owned_metadata_keys") == ["summary"]


# ---------------------------------------------------------------------------
# Path 2 — the bulk write. Driven end to end rather than at a seam: the merge
# is inline in ``create_memories_bulk``, and what matters is the metadata that
# lands on the ROW.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_write_leaves_a_caller_owned_summary_alone(_engine, monkeypatch):
    """The boundary must not depend on how a write was batched.

    ``MergeEnrichmentFields`` handles the single write and honoured C25; bulk
    items bypass the pipeline and went through their own merge, which wrote
    ``metadata["summary"]`` directly. So the same payload kept its summary as a
    single write and lost it as a batch of one.
    """
    from core_api.config import settings as core_settings
    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
    from core_api.services.memory_service import create_memories_bulk

    monkeypatch.setattr(core_settings, "deployment_mode", "inline")

    padding = " This memory carries enough surrounding context to pass the length gate."
    tenant = f"test-tenant-c25-{uuid.uuid4().hex[:8]}"
    request = BulkMemoryCreate(
        tenant_id=tenant,
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[
            BulkMemoryItem(
                content="The caller wrote their own summary for this row." + padding,
                # BOTH ownable keys: each call site wires ``caller_keys``
                # separately, so a summary-only payload leaves the ``tags``
                # site unexercised and a fix that missed it ships green.
                metadata={"summary": "MINE", "tags": ["mine"], "project": "apollo"},
            )
        ],
    )

    stub = AsyncMock(return_value=_enrichment())
    with (
        _enrichment_actually_runs(),
        patch("core_api.services.memory_enrichment.enrich_memory", new=stub),
    ):
        resp = await create_memories_bulk(request, bulk_attempt_id=uuid.uuid4().hex)

    # Guard against the vacuous pass: if enrichment never ran, the caller's
    # summary would survive because nothing touched it, and this test would
    # report success for a bug it cannot see.
    assert stub.await_count == 1, (
        f"enrichment did not run ({stub.await_count} calls) — the assertions below "
        "would pass whether or not the merge honours the boundary"
    )

    assert [r.status for r in resp.results] == ["created"], resp.results

    from sqlalchemy import text

    async with _engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT metadata FROM memories WHERE id = :i"),
                {"i": resp.results[0].id},
            )
        ).first()
    assert row is not None
    stored = row[0] or {}

    assert stored.get("summary") == "MINE", (
        "the caller's summary was overwritten by the bulk merge"
    )
    assert stored.get("tags") == ["mine"], (
        "the caller's tags were overwritten by the bulk merge"
    )
    assert SYSTEM_NAMESPACE in stored, "the platform namespace was never written"
    assert stored[SYSTEM_NAMESPACE].get("summary") == "PLATFORM SUMMARY", (
        "the platform's own summary must still be recorded under _system"
    )
    assert stored[SYSTEM_NAMESPACE].get("tags") == ["platform-tag"]
    assert stored.get("project") == "apollo", "unrelated caller keys must survive"


# ---------------------------------------------------------------------------
# The seam between the two halves.
#
# The forwarding tests above stop at ``publish_memory_enrich_request`` being
# CALLED, and the worker-side tests build the payload by hand. Nothing in
# either root proves the publisher actually serialises the field onto the wire,
# so the field could be dropped in the middle and both suites would stay green.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_publisher_puts_the_keys_on_the_wire_and_they_survive_the_round_trip():
    """Publish → payload → ``MemoryEnrichRequest`` — the worker's own first step.

    Asserted through a real deserialise rather than a dict lookup: the payload
    goes out as JSON, and a field the consumer model dropped (``extra="ignore"``
    is set on it) would still be present in the dict while never reaching the
    worker.
    """
    from common.events import memory_enrich_publisher
    from common.events.memory_enrich_request import MemoryEnrichRequest

    published: list = []

    class _Bus:
        async def publish(self, topic, event):
            published.append(event)

    with patch.object(memory_enrich_publisher, "get_event_bus", lambda: _Bus()):
        await memory_enrich_publisher.publish_memory_enrich_request(
            memory_id=uuid.uuid4(),
            content="body",
            tenant_id=TENANT,
            tenant_config=None,
            caller_owned_metadata_keys=["summary"],
        )

    assert len(published) == 1
    payload = published[0].payload
    assert "caller_owned_metadata_keys" in payload, (
        "the publisher dropped the field — the worker would never see it"
    )
    assert payload["caller_owned_metadata_keys"] == ["summary"]

    rebuilt = MemoryEnrichRequest(**payload)
    assert rebuilt.caller_owned_metadata_keys == ["summary"]


@pytest.mark.asyncio
async def test_the_wire_field_defaults_to_none_when_the_caller_owns_nothing():
    """A publisher that omits it (or an in-flight message from before this
    change) must deserialise to "caller owns nothing", not raise."""
    from common.events import memory_enrich_publisher
    from common.events.memory_enrich_request import MemoryEnrichRequest

    published: list = []

    class _Bus:
        async def publish(self, topic, event):
            published.append(event)

    with patch.object(memory_enrich_publisher, "get_event_bus", lambda: _Bus()):
        await memory_enrich_publisher.publish_memory_enrich_request(
            memory_id=uuid.uuid4(),
            content="body",
            tenant_id=TENANT,
            tenant_config=None,
        )

    payload = published[0].payload
    assert payload["caller_owned_metadata_keys"] is None
    assert MemoryEnrichRequest(**payload).caller_owned_metadata_keys is None

    payload.pop("caller_owned_metadata_keys")
    assert MemoryEnrichRequest(**payload).caller_owned_metadata_keys is None


# ---------------------------------------------------------------------------
# The duplicated policy set.
# ---------------------------------------------------------------------------


def test_the_worker_mirror_of_caller_ownable_keys_does_not_drift():
    """core-worker cannot import core-api, so it carries its own copy of
    ``CALLER_OWNABLE_KEYS``. This is the one pytest root that can import both,
    which makes it the only place the copies can be compared.

    Equality, not containment: a key ADDED to core-api's set but not the
    worker's would let the deferred path keep clobbering it — the bug this
    change closes, reopened for one field. A key added only to the WORKER's set
    would let a payload suppress a platform key's legacy write, which is the
    hole ``_record_metadata``'s intersection exists to prevent.
    """
    from core_api.services.system_metadata import CALLER_OWNABLE_KEYS
    from core_worker.consumer import _CALLER_OWNABLE_KEYS

    assert _CALLER_OWNABLE_KEYS == CALLER_OWNABLE_KEYS


def test_the_narrowing_helper_cannot_emit_a_non_ownable_key():
    """The publisher half of the same property: whatever a caller puts in
    ``metadata``, only ownable keys go on the wire. Without this, the worker's
    intersection would be the only thing standing between a caller-chosen key
    name and a suppressed platform write."""
    from core_api.services.system_metadata import CALLER_OWNABLE_KEYS

    data = SimpleNamespace(
        metadata={
            "summary": "mine",
            "contains_pii": False,
            "business_relevance": "personal",
            "enrichment_pending": True,
            "anything": 1,
        }
    )
    emitted = memory_service._caller_owned_enrichment_metadata_keys(data) or []
    assert set(emitted) <= CALLER_OWNABLE_KEYS
    assert emitted == ["summary"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_deferred_branch_publishes_the_callers_keys(_engine, monkeypatch):
    """The bulk path's OTHER branch — publish instead of merge.

    It matters more than it looks. The inline branch snapshots
    ``caller_keys`` BEFORE the merge; this branch re-derives the set from
    ``item.metadata`` AFTER, and that dict is mutated in place by the merge
    loop (``memory_type_agent_set``, ``weight_source``, and
    ``set_system_value`` itself). It is correct today only because
    ``defer_enrich_publish`` encodes ``not settings.inline_enrichment`` and is
    therefore mutually exclusive with enrichment having run — an invariant
    stated in a comment and, until now, tested nowhere. If it relaxes, the
    PLATFORM's summary would be published as caller-owned and the worker would
    skip the legacy mirror permanently.
    """
    from core_api.config import settings as core_settings
    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
    from core_api.services import memory_service as ms
    from core_api.services.memory_service import create_memories_bulk

    monkeypatch.setattr(core_settings, "deployment_mode", "deferred")

    padding = " This memory carries enough surrounding context to pass the length gate."
    request = BulkMemoryCreate(
        tenant_id=f"test-tenant-c25d-{uuid.uuid4().hex[:8]}",
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[
            BulkMemoryItem(
                content="Deferred bulk item with a caller summary." + padding,
                metadata={"summary": "MINE", "project": "apollo"},
            )
        ],
    )

    publish_spy = AsyncMock(return_value=None)
    with (
        _enrichment_actually_runs(),
        patch.object(ms, "publish_memory_enrich_request", new=publish_spy),
    ):
        resp = await create_memories_bulk(request, bulk_attempt_id=uuid.uuid4().hex)

    assert [r.status for r in resp.results] == ["created"], resp.results
    # ``call_count``, not ``await_count``: the publish is handed to
    # ``track_task(tracked_task(...))``, so the coroutine is constructed
    # eagerly (which is when the kwargs are bound) but awaited on the loop
    # later. Asserting on the await would race the background task.
    assert publish_spy.call_count == 1, (
        f"the deferred branch did not publish ({publish_spy.call_count} calls) — "
        "the assertion below would pass for the wrong reason"
    )
    assert publish_spy.call_args.kwargs["caller_owned_metadata_keys"] == ["summary"]
