"""The server-internal write paths must consult a dedup lookup (OSS #814).

Auto-chunk children (pipeline + legacy handlers) and the atomic-fact fanout each
attach a ``content_hash`` to every child and then inserted it unconditionally.
The public bulk path already dedups (``existing_hashes`` + ``seen_hashes``) and
the single-write path has ``CheckExactDuplicate``; these three had neither, which
is why prod carries duplicate content-hash groups with no concurrency involved:
18 groups / 19 surplus rows over 110k live rows, measured before this change.

Two distinct sources, tested separately because only one of them is something an
index could ever catch:

* already live — the content exists from an earlier call;
* repeated within one batch/fanout — the conflicting rows are both being written
  right now, so no constraint can collapse them; they have to be dropped before
  the write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.memory_enrichment import AtomicFact

# No ``asyncio`` mark: ``pytest.ini`` sets ``asyncio_mode = auto``, and applying
# it module-wide here warns on every sync test in the file.
pytestmark = [pytest.mark.unit]

TENANT = "t-child-dedup"
FLEET = "f1"
AGENT = "a"


def _fact(content: str) -> dict:
    return {"content": content, "suggested_type": "fact"}


def _drop(facts: list[dict], live: set[str]) -> list[dict]:
    return memory_service._drop_duplicate_facts(
        facts,
        tenant_id=TENANT,
        fleet_id=FLEET,
        live_hashes=live,
        source="auto_chunk",
    )


# ---------------------------------------------------------------------------
# _drop_duplicate_facts
# ---------------------------------------------------------------------------


def test_a_child_whose_content_is_already_live_is_dropped() -> None:
    live = {memory_service._content_hash(TENANT, FLEET, "alpha")}

    kept = _drop([_fact("alpha"), _fact("beta")], live)

    assert [f["content"] for f in kept] == ["beta"]


def test_a_child_repeated_within_the_batch_is_written_once() -> None:
    """No index can do this one: both rows are in the same INSERT."""
    kept = _drop([_fact("same"), _fact("same"), _fact("other")], set())

    assert [f["content"] for f in kept] == ["same", "other"]


def test_the_first_occurrence_is_the_one_kept() -> None:
    """Order matters: the survivor must be the earliest, so the row that lands
    is the one the chunker emitted first rather than an arbitrary member."""
    first, second = _fact("dup"), _fact("dup")
    first["marker"], second["marker"] = "first", "second"

    kept = _drop([first, second], set())

    assert [f["marker"] for f in kept] == ["first"]


def test_an_unhashable_child_is_never_dropped() -> None:
    """Empty content cannot collide, so it is outside the dedup contract and
    must survive — twice over."""
    kept = _drop([_fact(""), _fact("")], set())

    assert len(kept) == 2


def test_dropping_every_child_returns_empty_rather_than_raising() -> None:
    """A document re-chunked with every fact already live. The caller must see an
    empty list — it skips the storage roundtrip on it, and an INSERT with no
    VALUES is not valid SQL."""
    live = {memory_service._content_hash(TENANT, FLEET, "alpha")}

    assert _drop([_fact("alpha"), _fact("alpha")], live) == []


def test_the_scope_is_tenant_and_fleet_specific() -> None:
    """The hash folds in tenant and fleet, so a live hash from one fleet must not
    suppress the same text in another."""
    other_fleet_live = {memory_service._content_hash(TENANT, "other-fleet", "alpha")}

    kept = _drop([_fact("alpha")], other_fleet_live)

    assert [f["content"] for f in kept] == ["alpha"]


# ---------------------------------------------------------------------------
# _live_duplicate_hashes — the lookup's scope
# ---------------------------------------------------------------------------


async def test_the_lookup_is_scoped_to_tenant_fleet_and_agent() -> None:
    """Scope is the assertion, not an implementation detail. Dropping ``agent_id``
    would make two agents recording identical content collide, and dropping
    ``fleet_id`` would let one fleet's content suppress another's — the same
    scope ``memory_find_by_content_hash`` uses.
    """
    sc = MagicMock()
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={"h1": {"id": "x"}})

    result = await memory_service._live_duplicate_hashes(
        sc, tenant_id=TENANT, fleet_id=FLEET, agent_id=AGENT, hashes=["h1", "h2"]
    )

    assert result == {"h1"}
    sc.bulk_find_by_content_hashes.assert_awaited_once_with(
        TENANT, ["h1", "h2"], fleet_id=FLEET, agent_id=AGENT
    )


async def test_no_hashes_means_no_storage_roundtrip() -> None:
    sc = MagicMock()
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

    assert (
        await memory_service._live_duplicate_hashes(
            sc, tenant_id=TENANT, fleet_id=FLEET, agent_id=AGENT, hashes=[]
        )
        == set()
    )
    sc.bulk_find_by_content_hashes.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wiring: the atomic-fact fanout actually consults it
# ---------------------------------------------------------------------------


async def _run_fanout(facts: list[AtomicFact], live: dict[str, dict]) -> list[dict]:
    """Drive ``_enrich_memory_background``'s fanout, return the child writes.

    Same harness shape as ``test_atomic_fact_unembedded_persist``; the fanout is
    the most directly drivable of the three minting paths.
    """
    row = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        status="active",
        weight=0.5,
        ts_valid_start=None,
        ts_valid_end=None,
        metadata_={},
        deleted_at=None,
        fleet_id=FLEET,
        embedding=None,
        content="body",
        visibility="scope_team",
    )
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=row)
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.create_memory = AsyncMock(side_effect=lambda _p: {"id": str(uuid.uuid4())})
    sc.bulk_find_by_content_hashes = AsyncMock(return_value=live)

    enrichment = SimpleNamespace(
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
        atomic_facts=facts,
    )

    def _stub_tracked_task(coro, *_a, **_k):
        coro.close()
        return None

    async def _embed(_content, tenant_config=None, **_kwargs):
        return [0.0]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", new=_stub_tracked_task),
        patch.object(memory_service, "get_embedding", new=_embed),
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


async def test_fanout_skips_a_fact_that_is_already_recorded() -> None:
    alpha_hash = memory_service._content_hash(TENANT, FLEET, "alpha fact")

    children = await _run_fanout(
        [AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
        live={alpha_hash: {"id": str(uuid.uuid4()), "client_request_id": None}},
    )

    assert [c["content"] for c in children] == ["beta fact"], (
        "the fan-out re-wrote a fact it already had — this is one of the two "
        "paths that minted prod's duplicate groups"
    )


async def test_fanout_writes_a_repeated_fact_once() -> None:
    """An LLM emitting the same fact twice in one enrichment. Nothing is live, so
    the live lookup cannot help — this is the in-fanout case."""
    children = await _run_fanout(
        [AtomicFact(content="twice over"), AtomicFact(content="twice over")],
        live={},
    )

    assert len(children) == 1, "the same fact was written twice in one fan-out"


async def test_fanout_still_writes_everything_when_nothing_is_duplicated() -> None:
    """The guard against a dedup that eats legitimate facts."""
    children = await _run_fanout(
        [AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
        live={},
    )

    assert [c["content"] for c in children] == ["alpha fact", "beta fact"]


# ---------------------------------------------------------------------------
# Wiring: the auto-chunk children path consults it too
# ---------------------------------------------------------------------------


async def _run_auto_chunk(chunks: list[str], live: dict[str, dict]) -> list[dict]:
    """Drive ``_handle_auto_chunk_from_ctx``, return the child payloads written.

    The auto-chunk path is where duplicates are most likely to have come from:
    its children are all inserted in ONE ``create_memories`` call, so they share
    ``created_at`` exactly — which is the tie #839 had to add ``id`` to break.
    """
    from core_api.schemas import MemoryCreate

    parent_row = {
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
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value=live)

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="a body long enough to be worth chunking " * 5,
    )
    ctx = SimpleNamespace(
        data={
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=SimpleNamespace(entity_extraction_enabled=False),
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in chunks]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    if not sc.create_memories.await_args_list:
        return []
    return sc.create_memories.await_args_list[0].args[0]


async def test_the_parent_child_count_is_the_number_of_children_that_exist() -> None:
    """``child_count`` is written into the parent's metadata, so deduping after
    the parent insert would have made it permanently wrong — the field would
    claim children that were never written and nothing would ever correct it.
    Hence the dedup runs ahead of the parent insert.
    """
    from core_api.schemas import MemoryCreate

    parent_row = {
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
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    # Two of the three chunks are already recorded.
    sc.bulk_find_by_content_hashes = AsyncMock(
        return_value={
            memory_service._content_hash(TENANT, FLEET, "one"): {
                "id": str(uuid.uuid4()),
                "client_request_id": None,
            },
            memory_service._content_hash(TENANT, FLEET, "two"): {
                "id": str(uuid.uuid4()),
                "client_request_id": None,
            },
        }
    )

    data = MemoryCreate(
        tenant_id=TENANT, fleet_id=FLEET, agent_id=AGENT, content="body to chunk " * 10
    )
    ctx = SimpleNamespace(
        data={
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=SimpleNamespace(entity_extraction_enabled=False),
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in ("one", "two", "three")]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    parent_payload = sc.create_memory.await_args_list[0].args[0]
    assert parent_payload["metadata_"]["child_count"] == 1, (
        "child_count must count the children that will exist, not the chunks "
        "the chunker proposed"
    )

    written = sc.create_memories.await_args_list[0].args[0]
    assert [c["content"] for c in written] == ["three"]


async def test_a_dropped_child_is_never_embedded() -> None:
    """The embed is the expensive part of this path. Deduping after it would
    mean paying to vectorise text that is then thrown away."""
    from core_api.schemas import MemoryCreate

    embedded: list[list[str]] = []

    parent_row = {
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
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(
        return_value={
            memory_service._content_hash(TENANT, FLEET, "stale"): {
                "id": str(uuid.uuid4()),
                "client_request_id": None,
            }
        }
    )

    data = MemoryCreate(
        tenant_id=TENANT, fleet_id=FLEET, agent_id=AGENT, content="body to chunk " * 10
    )
    ctx = SimpleNamespace(
        data={
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=SimpleNamespace(entity_extraction_enabled=False),
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in ("stale", "fresh")]

    async def _embeddings(texts, _cfg, background=False):
        embedded.append(list(texts))
        return [[0.0] for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    assert embedded == [["fresh"]], f"embedded text the dedup had discarded: {embedded}"


async def test_auto_chunk_skips_a_child_whose_content_is_already_live() -> None:
    live_hash = memory_service._content_hash(TENANT, FLEET, "chunk one")

    children = await _run_auto_chunk(
        ["chunk one", "chunk two"],
        live={live_hash: {"id": str(uuid.uuid4()), "client_request_id": None}},
    )

    assert [c["content"] for c in children] == ["chunk two"]


async def test_auto_chunk_writes_a_repeated_chunk_once() -> None:
    children = await _run_auto_chunk(["same chunk", "same chunk", "other"], live={})

    assert [c["content"] for c in children] == ["same chunk", "other"]


async def test_auto_chunk_skips_the_storage_call_when_every_child_is_a_duplicate() -> None:
    """``create_memories([])`` would be a wasted roundtrip, and the storage-side
    statement would build an INSERT with no VALUES."""
    live_hash = memory_service._content_hash(TENANT, FLEET, "only chunk")

    children = await _run_auto_chunk(
        ["only chunk", "only chunk"],
        live={live_hash: {"id": str(uuid.uuid4()), "client_request_id": None}},
    )

    assert children == []


# ---------------------------------------------------------------------------
# Wiring: the LEGACY auto-chunk handler, which is the sibling of the above
# ---------------------------------------------------------------------------


async def _run_legacy_auto_chunk(chunks: list[str], live: dict[str, dict]) -> list[dict]:
    """Drive ``_create_memory_legacy``'s auto-chunk branch.

    Tested separately rather than assumed to match the pipeline handler: on two
    of this audit's last three findings the filed location was only half the
    problem, and the untested half was a sibling exactly like this one.
    """
    from core_api.schemas import MemoryCreate

    parent_row = {
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
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value=live)
    sc.find_embedding_by_content_hash = AsyncMock(return_value=None)
    sc.find_by_content_hash = AsyncMock(return_value=None)

    # Long enough to clear CHUNKING_THRESHOLD_CHARS, which gates the branch.
    from core_api.constants import CHUNKING_THRESHOLD_CHARS

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="x" * (CHUNKING_THRESHOLD_CHARS + 100),
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in chunks]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    async def _embedding(_content, _cfg=None, **_kw):
        return [0.0]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch.object(memory_service, "get_embedding", new=_embedding),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=False,
                    enrichment_provider="none",
                    entity_extraction_enabled=False,
                    auto_chunk_enabled=True,
                )
            ),
        ),
    ):
        await memory_service._create_memory_legacy(data)

    if not sc.create_memories.await_args_list:
        return []
    return sc.create_memories.await_args_list[0].args[0]


async def test_legacy_auto_chunk_skips_a_child_whose_content_is_already_live() -> None:
    live_hash = memory_service._content_hash(TENANT, FLEET, "legacy one")

    children = await _run_legacy_auto_chunk(
        ["legacy one", "legacy two"],
        live={live_hash: {"id": str(uuid.uuid4()), "client_request_id": None}},
    )

    assert [c["content"] for c in children] == ["legacy two"]


async def test_legacy_auto_chunk_writes_a_repeated_chunk_once() -> None:
    children = await _run_legacy_auto_chunk(["dup chunk", "dup chunk"], live={})

    assert [c["content"] for c in children] == ["dup chunk"]
