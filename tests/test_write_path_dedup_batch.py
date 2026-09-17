"""The write path's dedup gates: which ones run, in what order, and at what cost.

Eight audit findings that all land on the same mechanism — the point at which a
write discovers it is a duplicate, relative to the work it has already paid for.

* 08/14 L-33 — the exact-hash gate sat behind the embed, the enrichment and an
  entity upsert, so every duplicate paid for all three before being refused.
* 08/14 M-38 — the same shape on the bulk path, where a retried batch re-paid
  the whole provider budget for rows that were already stored.
* 09/02 M-47 — the auto-chunk branch is chosen on content LENGTH, before the
  write mode is resolved, so both of its exits applied strong-mode dedup
  whatever the caller asked for.
* 08/14 M-16 — and the multi-fact exit applied no SEMANTIC dedup to its parent
  at all, on any mode.
* 08/14 M-18 — that same exit handed a raw ``UUID`` to ``httpx``.
* 08/14 L-38 / 09/02 L-38 — the atomic-fact fan-out embedded one fact per
  provider call, and inflated a legal weight of 0.0 to 0.5 through ``or``.

The ordering assertions are deliberately about ORDER and not about counts: a
test that only asserted "the gate ran" passes just as well with the gate at the
end of the pipeline, which is the defect.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.pipeline.compositions import write as compositions
from core_api.schemas import MemoryCreate
from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit]

EMBEDDING_DIM = 1024

TENANT = "t-wpd"
FLEET = "f1"
AGENT = "a1"


def _step_names(pipeline) -> list[str]:
    return [s.name for s in pipeline._steps]


# ─────────────────────────── 08/14 L-33 — gate order ───────────────────────────


@pytest.mark.parametrize(
    "builder",
    [compositions.build_fast_write_pipeline, compositions.build_strong_write_pipeline],
)
def test_exact_dedup_gate_precedes_every_step_that_costs_something(builder) -> None:
    """The exact-hash gate runs as early as its input allows, on both modes.

    ``CheckExactDuplicate`` reads exactly one thing — ``ctx.data["content_hash"]``
    — so the earliest correct position is the statement after
    ``ComputeContentHash``. Everything it used to sit behind is work a duplicate
    paid for and threw away: an embedding call and an inline LLM enrichment
    (``ParallelEmbedEnrich``), and a storage entity UPSERT
    (``EmitMemoryTriple``).

    Asserted as a position relative to those steps rather than as a fixed index,
    so inserting an unrelated step does not fail this and moving the gate back
    behind the provider calls does.
    """
    names = _step_names(builder())
    gate = names.index("check_exact_duplicate")
    assert gate == names.index("compute_content_hash") + 1, (
        f"the exact-hash gate must run immediately after the hash it reads; got {names}"
    )
    for costly in ("parallel_embed_enrich", "emit_memory_triple"):
        assert gate < names.index(costly), (
            f"{costly} runs before the exact-dedup gate, so a duplicate pays for it: {names}"
        )


def test_governance_scan_still_precedes_the_hash() -> None:
    """Moving the dedup gate up must not have moved it above the masking step.

    ``GovernanceScanContent`` masks pattern-matched PII IN PLACE and is ahead of
    ``ComputeContentHash`` precisely so the hash — and therefore dedup, and
    therefore the stored row — sees the redacted content. A gate hoisted above
    it would dedup on the raw text and could 409 a write against a row whose
    content was never the same.
    """
    for builder in (
        compositions.build_fast_write_pipeline,
        compositions.build_strong_write_pipeline,
    ):
        names = _step_names(builder())
        assert names.index("governance_scan_content") < names.index(
            "compute_content_hash"
        )
        assert names.index("compute_content_hash") < names.index(
            "check_exact_duplicate"
        )


def test_persist_pipelines_differ_only_in_the_semantic_gate() -> None:
    """The fast/strong split on the auto-chunk fall-through is one step wide.

    Both fall-through pipelines are built from ``_persist_steps``, so this pins
    the property that made sharing it worthwhile: if the two ever diverge in
    anything but the gate, they have started drifting the way they did when the
    fall-through simply took the strong one.
    """
    strong = _step_names(compositions.build_persist_pipeline())
    fast = _step_names(compositions.build_fast_persist_pipeline())
    assert len(strong) == len(fast)
    differences = [(s, f) for s, f in zip(strong, fast, strict=True) if s != f]
    assert differences == [("check_semantic_duplicate", "detect_near_duplicate")], (
        differences
    )
    assert strong[0] == "check_exact_duplicate", strong


# ──────────────────── 08/14 M-18 / M-16 / 09/02 M-47 — auto-chunk ────────────────────


def _parent_row() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "fleet_id": FLEET,
        "agent_id": AGENT,
        "memory_type": "fact",
        "title": "t",
        "content": "body",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "created_at": datetime(2026, 9, 17, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }


class _AutoChunkRun:
    def __init__(self) -> None:
        self.parent_payload: dict | None = None
        self.persist_pipelines: list[str] = []
        self.semantic_lookups = 0


async def _drive_auto_chunk(
    *,
    write_mode: str,
    chunks: tuple[str, ...],
    subject_entity_id: uuid.UUID | None = None,
    semantic_hit: dict | None = None,
) -> _AutoChunkRun:
    """Drive ``_handle_auto_chunk_from_ctx`` and capture what it did.

    The mode travels on the ``MemoryCreate`` alone — the handler resolves it
    from that plus ``ctx.tenant_config``, which is the fix under test.
    """
    run = _AutoChunkRun()
    parent_row = _parent_row()

    sc = AsyncMock(name="storage_client")
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    sc.find_by_content_hash = AsyncMock(return_value=None)

    def _capture_create(payload):
        run.parent_payload = payload
        return parent_row

    sc.create_memory = AsyncMock(side_effect=_capture_create)

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="a body long enough to be worth chunking " * 60,
        subject_entity_id=subject_entity_id,
        write_mode=write_mode,
    )
    config = ResolvedConfig({"entity_extraction": {"enabled": False}})
    ctx = SimpleNamespace(
        data={
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "enrichment": None,
            # Real dimensionality: the fall-through's ``WriteMemoryRow`` inserts
            # into the test database, which enforces the vector width.
            "embedding": [0.1] * EMBEDDING_DIM,
            "t0": 0.0,
            # What ``ComputeContentHash`` leaves behind in
            # ``build_enrichment_pipeline``, which every caller of this handler
            # has already run. The fall-through pipeline's first step reads it.
            #
            # Unique per run, because the fall-through really does insert into
            # the test database: a shared constant here makes the SECOND
            # fall-through test 409 against the row the FIRST one wrote, which
            # reads exactly like the gate misfiring.
            "content_hash": uuid.uuid4().hex * 2,
        },
        tenant_config=config,
    )

    async def _chunk_content(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in chunks]

    async def _embeddings(texts, _cfg, background=False, **kw):
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    async def _semantic(*args, **kwargs):
        run.semantic_lookups += 1
        return semantic_hit

    real_persist = compositions.build_persist_pipeline
    real_fast_persist = compositions.build_fast_persist_pipeline

    def _spy_persist():
        run.persist_pipelines.append("strong")
        return real_persist()

    def _spy_fast_persist():
        run.persist_pipelines.append("fast")
        return real_fast_persist()

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service, "track_task", MagicMock(side_effect=close_scheduled_coro)
        ),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunk_content),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=_semantic,
        ),
        patch(
            "core_api.pipeline.compositions.write.build_persist_pipeline", _spy_persist
        ),
        patch(
            "core_api.pipeline.compositions.write.build_fast_persist_pipeline",
            _spy_fast_persist,
        ),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)
    return run


async def test_auto_chunk_parent_sends_subject_entity_id_as_a_string() -> None:
    """08/14 M-18 — the parent payload must survive ``json.dumps``.

    ``MemoryCreate.subject_entity_id`` is typed ``UUID``, the payload goes to
    ``httpx`` as ``json=``, and the stdlib encoder has no UUID support — so the
    raw value turned every auto-chunked write that named a subject entity into a
    ``TypeError`` the caller saw as a 500. Every other non-JSON-native field in
    the same dict literal was already converted, which is what made the omission
    easy to miss and total when it fired.

    Serialising the WHOLE payload rather than asserting on the one field: the
    field is the bug, but the payload surviving the encoder is the property.
    """
    subject = uuid.uuid4()
    run = await _drive_auto_chunk(
        write_mode="fast", chunks=("one", "two"), subject_entity_id=subject
    )
    assert run.parent_payload is not None
    assert run.parent_payload["subject_entity_id"] == str(subject)
    json.dumps(run.parent_payload)


async def test_fast_mode_fall_through_does_not_get_the_strong_semantic_gate() -> None:
    """09/02 M-47 — the caller's write mode survives the auto-chunk detour.

    The branch is entered on ``len(content) > CHUNKING_THRESHOLD_CHARS``, which
    is decided before ``_resolve_write_mode`` runs on the standard path. When
    the chunker then finds nothing to split, the write falls through to a
    persist pipeline — and that pipeline used to be the STRONG one
    unconditionally. So a ``write_mode="fast"`` caller could be 409-rejected by
    ``CheckSemanticDuplicate``, a gate whose entire purpose is to be opt-out,
    for no reason it could observe: the same content one byte shorter took the
    fast pipeline and landed.
    """
    run = await _drive_auto_chunk(write_mode="fast", chunks=("only one fact",))
    assert run.persist_pipelines == ["fast"], run.persist_pipelines


async def test_strong_mode_fall_through_keeps_the_strong_semantic_gate() -> None:
    """The other half of M-47: strong must not be downgraded either.

    Pinned alongside the fast case so a fix that simply swapped one hard-coded
    pipeline for the other fails here instead of passing the test above.
    """
    run = await _drive_auto_chunk(write_mode="strong", chunks=("only one fact",))
    assert run.persist_pipelines == ["strong"], run.persist_pipelines


async def test_strong_multi_fact_parent_is_semantically_deduped() -> None:
    """08/14 M-16 — the auto-chunk PARENT gets a semantic gate at last.

    The children have had an exact-hash gate since #841 and the parent gets a
    409 from ``_create_memory_or_409`` on an exact hash collision, but nothing
    asked whether the parent was a NEAR duplicate of a document already stored.
    Re-submitting the same report with one byte changed therefore produced a
    second parent and a second full set of children.

    The gate here is the real ``CheckSemanticDuplicate`` step object rather than
    a reimplementation of its thresholds, so this drives it through its own
    storage lookup.
    """
    hit = {"id": str(uuid.uuid4()), "similarity": 0.99, "status": "active"}
    with pytest.raises(Exception) as exc_info:
        await _drive_auto_chunk(
            write_mode="strong", chunks=("one", "two"), semantic_hit=hit
        )
    assert getattr(exc_info.value, "status_code", None) == 409, exc_info.value


async def test_strong_multi_fact_parent_without_a_near_duplicate_still_writes() -> None:
    """The gate must consult storage and then get out of the way.

    Without this, a gate that always raised — or one wired to the wrong
    similarity band — would pass the rejection test above while breaking every
    ordinary auto-chunked write.
    """
    run = await _drive_auto_chunk(write_mode="strong", chunks=("one", "two"))
    assert run.semantic_lookups == 1, run.semantic_lookups
    assert run.parent_payload is not None


async def test_fast_multi_fact_parent_is_not_marked_merged_by_a_gate_that_cannot_merge() -> (
    None
):
    """Fast-mode multi-fact parents stay ungated, and that is the deliberate part.

    Fast mode's gate is ``DetectNearDuplicate``, which does not refuse a write:
    on a hit it RECORDS a merge intent in ``ctx.data["merge_supersedes_id"]``
    and stamps ``metadata["near_duplicate_merged"] = True``, leaving
    ``ScheduleBackgroundTasks`` to perform the merge against the new row's id.
    This branch writes its parent by hand and has no such step, so borrowing
    that gate here would stamp a parent as merged while no merge ever happened —
    trading a missing gate for a false statement on the row.
    """
    run = await _drive_auto_chunk(
        write_mode="fast",
        chunks=("one", "two"),
        semantic_hit={"id": str(uuid.uuid4()), "similarity": 0.99, "status": "active"},
    )
    assert run.semantic_lookups == 0
    assert run.parent_payload is not None
    assert "near_duplicate_merged" not in run.parent_payload["metadata_"]


# ─────────────────────── 08/14 M-38 — bulk pays before it checks ───────────────────────


class _BulkRun:
    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.enriched: list[str] = []


async def _drive_bulk(items: list[dict], *, already_stored: dict[str, dict]) -> tuple:
    """Drive ``create_memories_bulk`` and record which items reached a provider.

    ``already_stored`` is keyed by CONTENT rather than by hash so a test can say
    "this text is a duplicate" without recomputing the tenant-scoped hash.
    """
    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem

    run = _BulkRun()
    data = BulkMemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        items=[BulkMemoryItem(**i) for i in items],
    )

    hash_of = {
        memory_service._content_hash(TENANT, FLEET, content): content
        for content in {i["content"] for i in items}
    }
    existing = {
        h: {"id": str(uuid.uuid4()), "client_request_id": None}
        for h, content in hash_of.items()
        if content in already_stored
    }

    sc = AsyncMock(name="storage_client")
    sc.bulk_find_by_content_hashes = AsyncMock(return_value=existing)
    sc.create_memories = AsyncMock(
        side_effect=lambda payloads: [
            {
                "id": str(uuid.uuid4()),
                "client_request_id": p["client_request_id"],
                "was_inserted": True,
            }
            for p in payloads
        ]
    )

    async def _embeddings(texts, _cfg, **kw):
        run.embedded.extend(texts)
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    async def _enrich(content, _cfg, **kw):
        run.enriched.append(content)
        return None

    # Enrichment ON explicitly, and the PROVIDER pinned with it. Both halves
    # are load-bearing: ``enrichment_enabled`` falls back to the global
    # ``use_llm_for_memory_creation`` (false here), and the bulk gate
    # additionally requires ``enrichment_provider != "none"``, which falls back
    # to the global ``entity_extraction_provider``. That global is "openai" by
    # default but ``none`` in CI (ci.yml sets ENTITY_EXTRACTION_PROVIDER), so
    # leaving it ambient makes these assertions pass locally and silently mean
    # nothing on the machine that gates the merge — ``assert enriched == []``
    # holds vacuously when no provider exists to enrich with. ``enrich_memory``
    # is patched below, so the name never reaches a client.
    config = ResolvedConfig(
        {
            "entity_extraction": {"enabled": False},
            "enrichment": {"enabled": True, "provider": "fake"},
        }
    )

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service, "track_task", MagicMock(side_effect=close_scheduled_coro)
        ),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=config),
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
        # ``inline_embedding`` / ``inline_enrichment`` are read-only properties
        # over ``deployment_mode``, so the mode is what has to move — the same
        # handle ``tests/test_bulk_write_mode.py`` uses.
        patch.object(memory_service.settings, "deployment_mode", "inline"),
    ):
        response = await memory_service.create_memories_bulk(
            data, bulk_attempt_id="attempt-1"
        )
    return response, run


async def test_a_fully_duplicate_batch_calls_no_provider_at_all() -> None:
    """08/14 M-38 — the dedup lookup moved ahead of the embed and the enrich.

    A retried batch is the ordinary outcome of a lost 200, and it is exactly
    what the per-attempt idempotency above this code is built to make safe. It
    was not made CHEAP: every item was embedded and enriched first, and only
    then did the content-hash lookup discover that every row was already
    stored. Nothing in either provider call feeds the lookup — the hash comes
    from ``item.content``, which governance has finished masking by then — so
    the whole budget was spent to produce results that were thrown away.
    """
    items = [{"content": "already stored one"}, {"content": "already stored two"}]
    response, run = await _drive_bulk(
        items, already_stored={"already stored one": {}, "already stored two": {}}
    )
    assert run.embedded == [], run.embedded
    assert run.enriched == [], run.enriched
    assert [r.status for r in response.results] == ["duplicate_content"] * 2


async def test_a_half_duplicate_batch_pays_only_for_the_new_rows() -> None:
    """The saving must be per item, not all-or-nothing."""
    items = [{"content": "already stored"}, {"content": "genuinely new"}]
    _response, run = await _drive_bulk(items, already_stored={"already stored": {}})
    assert run.embedded == ["genuinely new"], run.embedded
    assert run.enriched == ["genuinely new"], run.enriched


async def test_an_intra_batch_repeat_is_embedded_once() -> None:
    """The same content twice in one call is one row, and so one provider call.

    The classifier below already collapsed these to a single write; the
    providers did not know that and charged for both.
    """
    items = [{"content": "the same text twice"}, {"content": "the same text twice"}]
    response, run = await _drive_bulk(items, already_stored={})
    assert run.embedded == ["the same text twice"], run.embedded
    assert [r.status for r in response.results] == ["created", "duplicate_content"]


async def test_an_item_that_fails_validation_does_not_steal_a_valid_twin_s_embedding() -> (
    None
):
    """The first-occurrence scan must skip items that never reach the write.

    The classifier claims a hash (``seen_hashes[ch] = i``) only once an item has
    survived its validation and governance checks, so an item that errors out
    never becomes the first occurrence of its content — the NEXT item with the
    same text is the one that gets written. A pre-pass that walked every index
    instead would award the slot to the errored item, mark the real writer as an
    intra-batch duplicate, and skip its embedding: a vectorless row that
    persists, invisible to search until a backfill sweep finds it.

    This is the failure mode the optimisation introduces if it is written the
    obvious way, which is why it is pinned separately from the saving itself.
    """
    items = [
        {"content": "shared content text", "weight": 99.0},  # weight out of range
        {"content": "shared content text"},
    ]
    response, run = await _drive_bulk(items, already_stored={})
    statuses = [r.status for r in response.results]
    assert statuses[0] == "error", statuses
    assert statuses[1] == "created", statuses
    assert run.embedded == ["shared content text"], (
        "the surviving item must still be embedded; it is the row that persists"
    )


# ────────────── 08/14 L-38 / 09/02 L-38 — the atomic-fact fan-out ──────────────


class _FanoutRun:
    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []
        self.children: list[dict] = []


async def _drive_fanout(
    *,
    facts: list[tuple[str, str]],
    parent_weight,
    live_hashes: set[str] | None = None,
) -> _FanoutRun:
    run = _FanoutRun()
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(side_effect=lambda p: {"id": str(uuid.uuid4())})
    live = live_hashes or set()
    sc.bulk_find_by_content_hashes = AsyncMock(
        return_value={
            memory_service._content_hash(TENANT, FLEET, c): {"id": str(uuid.uuid4())}
            for c, _ in facts
            if c in live
        }
    )

    async def _embeddings(texts, _cfg, background=False, **kw):
        run.embed_calls.append(list(texts))
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    atomic = [
        SimpleNamespace(content=c, retrieval_hint=h, suggested_type="fact")
        for c, h in facts
    ]
    config = SimpleNamespace(enrichment_enabled=True, entity_extraction_enabled=False)

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service, "track_task", MagicMock(side_effect=close_scheduled_coro)
        ),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
    ):
        await memory_service.fan_out_atomic_facts(
            sc,
            atomic_facts=atomic,
            memory_id=uuid.uuid4(),
            tenant_id=TENANT,
            fleet_id=FLEET,
            agent_id=AGENT,
            parent_metadata={},
            parent_visibility="scope_team",
            parent_weight=parent_weight,
            parent_ts_start=None,
            tenant_config=config,
        )
    run.children = [c.args[0] for c in sc.create_memory.await_args_list]
    return run


async def test_the_fan_out_embeds_every_fact_in_one_provider_call() -> None:
    """08/14 L-38 — N facts, one embed call.

    The loop issued ``get_embedding`` per fact, serially, on a path that fires
    whenever the enricher finds more than one claim in a turn. The auto-chunk
    children had already been converted to a single batch call behind a degrade
    wrapper; the fan-out was the copy that had not.

    The INSERT deliberately stays per fact, and the next test pins why.
    """
    run = await _drive_fanout(
        facts=[
            ("fact one content", "h1"),
            ("fact two content", "h2"),
            ("fact three", "h3"),
        ],
        parent_weight=0.5,
    )
    assert run.embed_calls == [
        ["fact one content", "fact two content", "fact three"]
    ], run.embed_calls
    assert len(run.children) == 3


async def test_a_deduped_fact_is_never_handed_to_the_embedder() -> None:
    """Batching must not cost the property the per-fact ``continue`` protected.

    The old loop dropped a duplicate BEFORE reaching its embed call, so a fact
    already recorded cost nothing. Hoisting the embed above the dedup filter
    would be the obvious way to batch it and would quietly start paying for
    every duplicate — the very waste this batch of findings is about.
    """
    run = await _drive_fanout(
        facts=[("already recorded fact", "h1"), ("genuinely new fact", "h2")],
        parent_weight=0.5,
        live_hashes={"already recorded fact"},
    )
    assert run.embed_calls == [["genuinely new fact"]], run.embed_calls
    assert [c["content"] for c in run.children] == ["genuinely new fact"]


async def _children_of_background_enrich(
    *, row_weight, enrichment_weight
) -> list[dict]:
    """Drive ``_enrich_memory_background`` and return the child payloads.

    Goes through the real call site rather than handing ``fan_out_atomic_facts``
    a ready-made ``parent_weight``: the ``or`` chain being tested lives at the
    call site, so a test that passes the weight in directly cannot see it and
    would pass against the unfixed code.
    """
    from common.enrichment.schema import AtomicFact

    row = {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": row_weight,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "fleet_id": FLEET,
        "embedding": None,
        "content": "body",
        "visibility": "scope_team",
    }
    enrichment = SimpleNamespace(
        memory_type="fact",
        weight=enrichment_weight,
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
        atomic_facts=[
            AtomicFact(content="alpha fact"),
            AtomicFact(content="beta fact"),
        ],
    )

    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=row)
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

    async def _embeddings(texts, _cfg, background=False, **kw):
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service, "track_task", MagicMock(side_effect=close_scheduled_coro)
        ),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
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
            uuid.uuid4(), "body", TENANT, FLEET, AGENT
        )
    return [c.args[0] for c in sc.create_memory.await_args_list]


async def test_an_all_duplicate_fan_out_calls_the_embedder_not_at_all() -> None:
    """Batching must not turn "nothing to embed" into a provider call.

    ``get_embeddings_batch`` has no empty-input guard: an empty list reaches the
    provider as ``{"input": []}``, is rejected, and lands in ``record_failure``
    — advancing the bulk-failure streak that trips the degraded-provider wire.
    So a re-enrichment of a parent whose facts are ALL already recorded, which
    is precisely the case the live-hash lookup exists for, would have reported a
    provider failure for doing exactly the right thing. The per-fact loop this
    batching replaced made zero calls here, and so must this.
    """
    run = await _drive_fanout(
        facts=[("already recorded one", "h1"), ("already recorded two", "h2")],
        parent_weight=0.5,
        live_hashes={"already recorded one", "already recorded two"},
    )
    assert run.embed_calls == [], run.embed_calls
    assert run.children == []


async def test_a_parent_weight_of_zero_is_not_rounded_up_to_the_default() -> None:
    """09/02 L-38 — 0.0 is a legal weight, and ``or`` cannot carry it.

    ``weight`` is declared ``ge=0.0``, so zero means "keep this but never
    surface it" — a deliberate suppression. Both fan-out call sites chained
    ``... or 0.5``, so every child of a suppressed parent was born at 0.5:
    precisely the rows an operator had just finished turning down, silently
    turned back up, and only for content the enricher split into multiple
    claims.

    Driven with the STORED weight at zero and no enrichment weight, which is the
    arrangement the two implementations disagree on: the chain reads
    ``None or 0.0 or 0.5`` and lands on the default, while the fix takes the
    stored zero. Picking the arrangement matters — with a non-default row weight
    the enricher's value never reaches ``patch`` at all (``_agent_pinned``
    treats anything but 0.5 as caller-set), so both implementations would agree
    and the test would pass either way.
    """
    children = await _children_of_background_enrich(
        row_weight=0.0, enrichment_weight=None
    )
    assert children, "the fan-out must have written children to assert on"
    assert [c["weight"] for c in children] == [0.0, 0.0], children


async def test_the_worker_fan_out_also_keeps_a_zero_parent_weight() -> None:
    """The consumer's call site, which had the same ``or`` and its own copy of it.

    ``_fan_out_persisted_atomic_facts`` is how the DEFERRED deployment creates
    these children — the worker stores the facts and this consumer writes the
    rows — so leaving it on the falsy chain would have kept the bug alive for
    every tenant that defers enrichment, which is the SaaS default.
    """
    from core_api import consumer

    row = {
        "id": str(uuid.uuid4()),
        "fleet_id": FLEET,
        "agent_id": AGENT,
        "weight": 0.0,
        "visibility": "scope_team",
        "ts_valid_start": None,
        "metadata_": {"atomic_facts": [{"content": "alpha fact"}]},
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    sc.update_memory = AsyncMock(return_value=None)

    async def _embeddings(texts, _cfg, background=False, **kw):
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    payload = SimpleNamespace(memory_id=uuid.uuid4(), tenant_id=TENANT)

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service, "track_task", MagicMock(side_effect=close_scheduled_coro)
        ),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
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
        await consumer._fan_out_persisted_atomic_facts(
            sc, row, payload, SimpleNamespace(visibility=None)
        )

    children = [c.args[0] for c in sc.create_memory.await_args_list]
    assert children, "the worker fan-out must have written a child to assert on"
    assert [c["weight"] for c in children] == [0.0], children


def test_the_weight_resolver_prefers_the_first_stated_value() -> None:
    """The resolver's own contract, including the case ``or`` gets wrong.

    Driven directly as well as through the fan-out because the ordering rule —
    "the weight this write is setting, else the row's current one, else the
    default" — is what both call sites rely on, and a fan-out test can only
    reach one arrangement of it at a time.
    """
    resolve = memory_service._resolve_parent_weight
    assert resolve(0.0, 0.9) == 0.0, "a stated 0.0 must win over the stored value"
    assert resolve(None, 0.0) == 0.0, "a stored 0.0 must win over the default"
    assert resolve(None, None) == 0.5
    assert resolve(0.7, 0.2) == 0.7
    assert resolve(None, 0.2) == 0.2
