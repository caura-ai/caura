"""A rejected duplicate insert must surface as 409, not 500 (OSS #814).

Migration 040 gave ``(tenant, fleet, agent, content_hash)`` a partial unique
index, so an insert of duplicate content is now REFUSED where it previously
succeeded and quietly created a second row. That refusal has to land on the code
the dedup contract already uses: ``CheckExactDuplicate`` raises 409 for the
duplicate it can see before the write, and this is the same answer for the race
it cannot see — the gate looked, found nothing, and a concurrent writer committed
in between.

Untranslated it is a 500. The pipeline marks the step FAILED and the caller reads
"Memory write pipeline failed unexpectedly", which says nothing about which row
to use instead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from common import duplicate_memory
from fastapi import HTTPException

from core_api.clients.storage_client import (
    DuplicateMemoryError,
    _storage_detail,
)
from core_api.services import memory_service

pytestmark = [pytest.mark.unit]

WINNER = str(uuid.uuid4())


def _response(status: int, body) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body,
        request=httpx.Request("POST", "http://storage/memories"),
    )


# ---------------------------------------------------------------------------
# _storage_detail — the id has to survive the trip
# ---------------------------------------------------------------------------


def test_the_winning_row_id_is_carried_through() -> None:
    """An agent told "duplicate" without being told WHICH row cannot use the row
    it should have got. The id is the useful half of the message."""
    detail = _storage_detail(_response(409, {"detail": f"Duplicate memory exists: {WINNER}"}))

    assert WINNER in detail


@pytest.mark.parametrize(
    "body",
    [
        {"detail": None},
        {"detail": ""},
        {"detail": {"nested": "object"}},
        {"other": "key"},
        [],
    ],
    ids=["null", "empty", "non-string", "missing", "not-a-dict"],
)
def test_a_malformed_body_still_yields_a_usable_message(body) -> None:
    """This runs on an error path, so it must not raise. A 409 whose body is not
    the expected shape must still produce a 409 with something readable, rather
    than a TypeError from inside the exception handler."""
    assert _storage_detail(_response(409, body)) == "Duplicate memory exists"


def test_a_non_json_body_still_yields_a_usable_message() -> None:
    resp = httpx.Response(
        status_code=409,
        content=b"<html>gateway</html>",
        request=httpx.Request("POST", "http://storage/memories"),
    )

    assert _storage_detail(resp) == "Duplicate memory exists"


# ---------------------------------------------------------------------------
# The client raises the typed error, and ONLY for 409
# ---------------------------------------------------------------------------


async def _client_create(status: int, body) -> None:
    from core_api.clients.storage_client import CoreStorageClient

    client = CoreStorageClient()
    err = httpx.HTTPStatusError(
        "upstream", request=_response(status, body).request, response=_response(status, body)
    )
    with patch.object(CoreStorageClient, "_post", new=AsyncMock(side_effect=err)):
        await client.create_memory({"agent_id": "a", "tenant_id": "t"})


@pytest.mark.asyncio
async def test_a_storage_409_becomes_the_typed_duplicate_error() -> None:
    with pytest.raises(DuplicateMemoryError) as caught:
        await _client_create(409, {"detail": f"Duplicate memory exists: {WINNER}"})

    assert WINNER in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422, 500, 503])
async def test_every_other_status_propagates_unchanged(status) -> None:
    """Narrow on purpose. A 500 must keep reaching the app's upstream handler,
    which turns it into a retryable 503; swallowing it here would relabel a
    storage outage as a duplicate."""
    with pytest.raises(httpx.HTTPStatusError):
        await _client_create(status, {"detail": "something else"})


# ---------------------------------------------------------------------------
# The service helper turns it into the 409 the contract promises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_helper_maps_the_duplicate_to_409_with_the_id() -> None:
    """The message-only case, which is also the older-storage case (C29).

    ``DuplicateMemoryError`` carries structured fields now, but a storage that
    predates them sends only the sentence — as constructed here. The helper must
    still produce a 409 that names the winning row, with an empty ``details``
    rather than a failure, or the two services could not deploy independently.
    """
    sc = MagicMock()
    sc.create_memory = AsyncMock(
        side_effect=DuplicateMemoryError(f"Duplicate memory exists: {WINNER}")
    )

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        with pytest.raises(HTTPException) as caught:
            await memory_service._create_memory_or_409({"tenant_id": "t"})

    assert caught.value.status_code == 409
    # ``detail`` is the structured shape now; the id lives in the message, and
    # ``app.http_exception_handler`` is what flattens it back to a plain string
    # for the top-level ``detail`` field clients read.
    assert WINNER in caught.value.detail["message"]
    assert caught.value.detail["code"] == duplicate_memory.DUPLICATE_MEMORY_CODE
    assert caught.value.detail["details"] == {}


@pytest.mark.asyncio
async def test_the_helper_forwards_storage_structured_fields() -> None:
    """The current-storage case: the id arrives as data and is passed through
    rather than being re-derived from the sentence."""
    sc = MagicMock()
    sc.create_memory = AsyncMock(
        side_effect=DuplicateMemoryError(
            f"Duplicate memory exists: {WINNER}",
            {
                "reason": duplicate_memory.REASON_EXACT,
                "existing_id": WINNER,
                "existing_status": "active",
            },
        )
    )

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        with pytest.raises(HTTPException) as caught:
            await memory_service._create_memory_or_409({"tenant_id": "t"})

    details = caught.value.detail["details"]
    assert details["existing_id"] == WINNER
    assert details["existing_status"] == "active"
    assert details["reason"] == duplicate_memory.REASON_EXACT


@pytest.mark.asyncio
async def test_the_helper_passes_a_successful_write_straight_through() -> None:
    sc = MagicMock()
    sc.create_memory = AsyncMock(return_value={"id": WINNER})

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        assert await memory_service._create_memory_or_409({"tenant_id": "t"}) == {"id": WINNER}


@pytest.mark.asyncio
async def test_the_helper_does_not_swallow_other_failures() -> None:
    """The guard against a helper that turns every write failure into a 409."""
    sc = MagicMock()
    sc.create_memory = AsyncMock(side_effect=RuntimeError("storage exploded"))

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        with pytest.raises(RuntimeError):
            await memory_service._create_memory_or_409({"tenant_id": "t"})


# ---------------------------------------------------------------------------
# The main write path: WriteMemoryRow turns it into the caller's 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_write_pipeline_returns_409_not_a_pipeline_failure() -> None:
    """The end-to-end claim. ``CheckExactDuplicate`` runs earlier in this same
    pipeline and found nothing, so a refusal at the insert is the race — and it
    has to reach the caller as the same 409 that gate would have raised.

    Without the translation the step is marked FAILED and the runner converts it
    to "Memory write pipeline failed unexpectedly" (500), which tells the caller
    nothing about which row now owns the content.
    """
    from core_api.clients import storage_client as sc_mod

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.schemas import MemoryCreate
        from core_api.services.memory_service import create_memory

        refuse = AsyncMock(
            side_effect=DuplicateMemoryError(f"Duplicate memory exists: {WINNER}")
        )
        with patch.object(sc_mod.CoreStorageClient, "create_memory", new=refuse):
            with pytest.raises(HTTPException) as caught:
                await create_memory(
                    MemoryCreate(
                        tenant_id="t-pipeline-409",
                        fleet_id="f1",
                        agent_id="a",
                        content=(
                            "content long enough to clear the quality gate on the "
                            f"write pipeline for this duplicate-race test {uuid.uuid4().hex}"
                        ),
                    )
                )
    finally:
        memory_service._USE_PIPELINE_WRITE = original

    assert caught.value.status_code == 409, (
        f"a duplicate race surfaced as {caught.value.status_code}, not the 409 the "
        "dedup contract promises"
    )
    assert WINNER in str(caught.value.detail)


# ---------------------------------------------------------------------------
# The fanout treats it as "already recorded", not as an error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fanout_treats_a_duplicate_as_recorded_and_keeps_going() -> None:
    """The fan-out has no HTTP contract to honour — it runs in a fire-and-forget
    task, so a 409 would go nowhere and would abort the facts after it. A refusal
    there means the fact is already stored, which is the outcome it wants.

    The second fact must still be written: that is the "keeps going" half, and it
    is what a bare ``raise`` would break.
    """
    from core_api.services.memory_enrichment import AtomicFact

    tenant, fleet, agent = "t-dup409", "f1", "a"
    row = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        status="active",
        weight=0.5,
        ts_valid_start=None,
        ts_valid_end=None,
        metadata_={},
        deleted_at=None,
        fleet_id=fleet,
        embedding=None,
        content="body",
        visibility="scope_team",
    )
    first_hash = memory_service._content_hash(tenant, fleet, "alpha fact")

    async def _create(payload):
        # Only the first fact is refused, so the loop has to survive it and go on
        # to write the second.
        if payload["content_hash"] == first_hash:
            raise DuplicateMemoryError(f"Duplicate memory exists: {WINNER}")
        return {"id": str(uuid.uuid4())}

    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=row)
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.create_memory = AsyncMock(side_effect=_create)
    # Empty: the point here is the WRITE being refused, not the lookup catching
    # it. A non-empty return would skip the fact before it ever reached storage
    # and the test would prove nothing about the 409 path.
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

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
        atomic_facts=[AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
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
        # Must not raise: a DuplicateMemoryError escaping the loop would surface
        # as a failed background task.
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "body", tenant, fleet, agent
        )

    attempted = [c.args[0]["content"] for c in sc.create_memory.await_args_list]
    assert attempted == ["alpha fact", "beta fact"], (
        "the refusal aborted the rest of the fan-out instead of being treated as "
        f"already-recorded: {attempted}"
    )


# ---------------------------------------------------------------------------
# The bulk child insert must DEGRADE — the parent is already committed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_child_batch_does_not_strand_the_committed_parent() -> None:
    """The H-05 shape (#815), reachable again through the new constraint.

    The auto-chunk parent is committed BEFORE its children, and the child insert
    is one statement that a single duplicate aborts. Raising would hand the caller
    a 500 for a write that persisted and leave the parent childless with no record
    of why.

    Degrading loses nothing in this specific case: the constraint refuses the
    batch only when that content is already stored, so the children this call
    would have written already exist.
    """
    caught: list[str] = []

    async def _refuse(_payloads):
        raise DuplicateMemoryError("bulk insert rejected: an item's content already exists")

    sc = MagicMock()
    sc.create_memories = AsyncMock(side_effect=_refuse)

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(
            memory_service.logger, "warning", lambda msg, **kw: caught.append(msg)
        ),
    ):
        # Must not raise.
        await memory_service._insert_children_or_degrade(
            [{"content_hash": "h1"}, {"content_hash": "h2"}],
            tenant_id="t",
            parent_id=WINNER,
            source="auto_chunk",
        )

    assert caught, "the dropped children left no trace at all"
    assert "parent kept without them" in caught[0]


@pytest.mark.asyncio
async def test_every_other_child_insert_failure_still_raises() -> None:
    """The guard against a degrade that hides real loss. Only a duplicate refusal
    is safe to swallow — for anything else the rows genuinely are missing."""
    sc = MagicMock()
    sc.create_memories = AsyncMock(side_effect=RuntimeError("storage exploded"))

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        with pytest.raises(RuntimeError):
            await memory_service._insert_children_or_degrade(
                [{"content_hash": "h1"}],
                tenant_id="t",
                parent_id=WINNER,
                source="auto_chunk",
            )


@pytest.mark.asyncio
async def test_an_empty_child_list_makes_no_storage_call() -> None:
    """Dedup can empty the list, and ``create_memories([])`` builds an INSERT with
    no VALUES."""
    sc = MagicMock()
    sc.create_memories = AsyncMock()

    with patch.object(memory_service, "get_storage_client", lambda: sc):
        await memory_service._insert_children_or_degrade(
            [], tenant_id="t", parent_id=WINNER, source="auto_chunk"
        )

    sc.create_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_bulk_client_translates_a_409_the_same_way() -> None:
    """Both endpoints have to agree about what a duplicate looks like, or a
    core-api caller sees a different failure depending only on which one it used.
    """
    from core_api.clients.storage_client import CoreStorageClient

    resp = _response(409, {"detail": "bulk insert rejected: an item's content already exists"})
    err = httpx.HTTPStatusError("upstream", request=resp.request, response=resp)

    with patch.object(CoreStorageClient, "_post", new=AsyncMock(side_effect=err)):
        with pytest.raises(DuplicateMemoryError) as caught:
            await CoreStorageClient().create_memories([{"agent_id": "a", "tenant_id": "t"}])

    assert "already exists" in str(caught.value)


@pytest.mark.asyncio
async def test_the_bulk_client_still_propagates_other_statuses() -> None:
    from core_api.clients.storage_client import CoreStorageClient

    resp = _response(503, {"detail": "upstream down"})
    err = httpx.HTTPStatusError("upstream", request=resp.request, response=resp)

    with patch.object(CoreStorageClient, "_post", new=AsyncMock(side_effect=err)):
        with pytest.raises(httpx.HTTPStatusError):
            await CoreStorageClient().create_memories([{"agent_id": "a", "tenant_id": "t"}])
