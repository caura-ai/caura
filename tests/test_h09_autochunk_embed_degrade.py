"""H-09 — the auto-chunk child embed raised after the parent had committed.

``_handle_auto_chunk_from_ctx`` commits the parent, then batch-embeds the
children. ``get_embeddings_batch`` raises on every provider-side error, gate
saturation, quota and misconfig — its own docstring notes both bulk callers
wrap it — and this third caller did not. The call also sits OUTSIDE the
request's ``try/finally``, because the auto-chunk branch returns before that
block is entered, so the exception escaped as an error response for a write
that had already persisted:

* the parent's metadata claimed ``auto_chunked`` and ``child_count=N`` while
  zero children existed, so the facts were silently lost and the source
  document was unrecallable;
* none of the backfills below the embed ran — parent enrich, entity
  extraction, parent embed — so a parent inserted with ``embedding=None``
  stayed unembedded;
* and the retry WEDGED. Re-chunking produces the same full-document
  ``content_hash``, migration 040's live-row uniqueness answers 409 against
  the childless parent, and the children could never be written without
  deleting the parent by hand.

Two asymmetries make it unambiguous rather than a judgement call. The parent's
own embed already degrades to ``None`` on these same failures. And the child
INSERT one line later already degrades for precisely this reason — its
docstring reads "the parent row is ALREADY COMMITTED... raising here would hand
the caller a 500 for a write that persisted". The child EMBED between them was
the only step that still raised.

The fix degrades: children persist unembedded, carry ``embedding_pending``, and
each gets a re-embed queued. Deliberately NOT mode-dependent like the bulk
path, which fails the request under ``inline_embedding`` — there nothing has
persisted when the embed fails, so refusing is clean; here the parent is
committed, which inverts the trade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.clients.storage_client import DuplicateMemoryError
from core_api.schemas import MemoryCreate
from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig

pytestmark = [pytest.mark.unit]

TENANT = "t-h09"
FLEET = "f1"
AGENT = "a"
CHUNKS = ("chunk one", "chunk two", "chunk three")


class EmbedDown(RuntimeError):
    """Stands in for the whole class: provider error, gate timeout, quota, misconfig."""


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
        "created_at": datetime(2026, 9, 4, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }


async def _noop() -> None:
    """A real coroutine, so ``tracked_task``'s ``coro.close()`` stays honest."""
    return None


class _Run:
    def __init__(self) -> None:
        self.parent_id = ""
        self.parent_payload: dict = {}
        self.children: list[dict] = []
        self.tasks: list[tuple[str, str]] = []
        self.raised: BaseException | None = None
        # crid -> the id the fake bulk insert assigned to that payload.
        self.assigned_ids: dict[str, str] = {}
        # (memory_id, content, content_hash) per scheduled re-embed, captured at
        # CALL time — the coroutine is never awaited, so an async double would
        # record nothing.
        self.reembeds: list[tuple[str, str, str]] = []

    @property
    def labels(self) -> set[str]:
        return {label for label, _ in self.tasks}

    def scheduled_for(self, label: str) -> set[str]:
        return {mid for lbl, mid in self.tasks if lbl == label}


async def _run(
    *,
    embed_raises: bool,
    return_ids: bool = True,
    inline: bool = True,
    legacy: bool = False,
    rotate_results: bool = False,
    duplicate_refusal: bool = False,
    bad_id_index: int | None = None,
) -> _Run:
    """Drive the multi-fact auto-chunk exit.

    ``embed_raises`` makes the CHILD batch embed fail the way a degraded
    provider does. ``return_ids`` controls whether the bulk insert reports a
    usable id per row, which is the difference between a queued repair and the
    loud no-id log. ``legacy`` flips ``_USE_PIPELINE_WRITE`` and enters through
    ``create_memory``, so the rollback path is exercised through the real
    dispatch rather than by calling its private handler.

    ``rotate_results`` returns the bulk-insert results in an order that matches
    no payload position, to pin the payload-to-result join. Storage guarantees
    input order today, so this is a contract-drift probe rather than a
    reproduction of live behaviour — see the shuffled-order test.

    ``duplicate_refusal`` makes the child insert answer migration 040's 409, so
    NOTHING is written. Combined with ``embed_raises`` it reproduces the
    retry-after-degradation case: the provider is still down and the children
    already exist from an earlier attempt.

    ``bad_id_index`` gives ONE child a truthy but unparseable id, so it clears
    the no-id branch and reaches ``UUID()``. One rather than all, so the test
    can tell a per-child guard from one wrapped around the whole loop.
    """
    run = _Run()
    parent_row = _parent_row()
    run.parent_id = parent_row["id"]

    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    # The legacy path runs a dedup pre-check the pipeline path does not. A bare
    # AsyncMock answers it with a truthy mock, which the handler reads as "this
    # content already exists" and 409s before ever reaching the embed — so
    # without this the legacy tests fail on a duplicate that does not exist.
    sc.find_by_content_hash = AsyncMock(return_value=None)

    async def _create_memories(payloads):
        if duplicate_refusal:
            raise DuplicateMemoryError("a live row already holds this content")
        # Mirrors the documented contract: one entry per input payload,
        # ``id`` populated regardless of who committed the row.
        out = []
        for i, p in enumerate(payloads):
            mem_id = str(uuid.uuid4()) if return_ids else None
            if i == bad_id_index:
                # Truthy, so it clears the no-id branch, but not parseable —
                # the one shape that reaches ``UUID()``.
                mem_id = "not-a-uuid"
            elif mem_id:
                run.assigned_ids[p["client_request_id"]] = mem_id
            out.append(
                {
                    "client_request_id": p["client_request_id"],
                    "id": mem_id,
                    "was_inserted": True,
                }
            )
        if rotate_results:
            # Rotate rather than reverse: reversing an odd-length list leaves
            # the middle element on its own index, so a positional join would
            # still pair one child correctly and the test could read as a
            # partial pass. A rotation moves every element.
            out = out[1:] + out[:1]
        return out

    sc.create_memories = AsyncMock(side_effect=_create_memories)

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="a body long enough to be worth chunking " * 60,
    )
    config = ResolvedConfig(
        {
            "chunking": {"auto_chunk_enabled": True},
            "entity_extraction": {"enabled": False},
            "enrichment": {"enabled": False},
        }
    )
    ctx = SimpleNamespace(
        data={
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": None,
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "enrichment": None,
            "embedding": [0.0] * 8 if inline else None,
            "t0": 0.0,
        },
        tenant_config=config,
    )

    async def _chunk_content(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in CHUNKS]

    async def _embeddings(texts, _cfg, background=False):
        if embed_raises:
            raise EmbedDown("provider degraded")
        return [[0.0] * 8 for _ in texts]

    def _tracked_task(coro, label, memory_id, tenant_id, *a, **k):
        run.tasks.append((label, str(memory_id)))
        coro.close()
        return MagicMock()

    def _schedule(memory_id, content, _tenant_id, *, content_hash=None, **k):
        # Sync on purpose: records when CALLED, which is the only moment that
        # happens here — ``_tracked_task`` closes the coroutine unawaited.
        run.reembeds.append((str(memory_id), content, content_hash))
        return _noop()

    with (
        patch.object(
            memory_service.settings,
            "deployment_mode",
            "inline" if inline else "deferred",
        ),
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "tracked_task", _tracked_task),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch.object(memory_service, "_schedule_embed_or_reembed", new=_schedule),
        patch("core_api.services.ingest_service._chunk_content", new=_chunk_content),
        patch.object(memory_service, "_USE_PIPELINE_WRITE", not legacy),
        # Both dedup pre-checks are legacy-path-only and answer truthy off a
        # bare AsyncMock, which the handler reads as "already exists" and 409s
        # before reaching the embed. Neither is what these tests are about.
        patch.object(
            memory_service, "_find_semantic_duplicate", AsyncMock(return_value=None)
        ),
        # The legacy handler resolves its OWN config rather than taking the
        # ctx one, so ``auto_chunk_enabled`` has to be supplied here or the
        # branch under test is never entered and the run looks like a pass.
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=config),
        ),
    ):
        try:
            if legacy:
                # Through the public entry point, so the dispatch that selects
                # the rollback handler is part of what is under test.
                await memory_service.create_memory(data)
            else:
                await memory_service._handle_auto_chunk_from_ctx(data, ctx)
        except Exception as exc:
            # Captured rather than propagated: the escape IS the defect, so the
            # tests assert on ``run.raised`` instead of wrapping each call in
            # ``pytest.raises`` and inverting it per case.
            run.raised = exc

    if sc.create_memories.await_args_list:
        run.children = sc.create_memories.await_args_list[0].args[0]
    if sc.create_memory.await_args_list:
        run.parent_payload = sc.create_memory.await_args_list[0].args[0]
    return run


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


async def test_a_failed_child_embed_does_not_escape_the_request() -> None:
    """THE DEFECT. The parent is already committed when this runs.

    Fails without the fix with ``EmbedDown`` propagating out of
    ``_handle_auto_chunk_from_ctx`` — an error response for a persisted write.
    """
    run = await _run(embed_raises=True)
    assert run.raised is None, (
        f"child embed failure escaped as {type(run.raised).__name__}: {run.raised}"
    )


async def test_the_children_are_still_written_when_the_embed_fails() -> None:
    """The facts must survive. This is the silent data loss half.

    Without the fix zero children were written while the parent's metadata
    claimed ``child_count=3``, so the source document became unrecallable and
    nothing recorded why.
    """
    run = await _run(embed_raises=True)
    assert len(run.children) == len(CHUNKS)
    assert [c["content"] for c in run.children] == list(CHUNKS)
    # Unembedded, not silently zero-vectored — a zero vector would be a row
    # that looks embedded and never matches anything.
    assert all(c["embedding"] is None for c in run.children)


async def test_unembedded_children_are_marked_pending() -> None:
    """``embedding_pending`` is public API, not bookkeeping.

    ``MemoryOut.metadata`` documents an ABSENT flag as "that stage ran
    inline", so leaving it off states the opposite of the truth for these rows
    and makes them indistinguishable from fully-embedded ones.
    """
    run = await _run(embed_raises=True)
    # Anti-vacuity, and it earned its place: ``all()`` over an empty list is
    # True, so without this the test PASSED with the guard removed — the
    # pre-fix behaviour writes no children at all. Caught by the removal probe.
    assert len(run.children) == len(CHUNKS), "no children written — test is vacuous"
    assert all(c["metadata_"].get("embedding_pending") is True for c in run.children)


async def test_each_unembedded_child_gets_a_re_embed_queued() -> None:
    """Scheduled explicitly, because the sweep is the floor and not the mechanism.

    ``embed_backfill_enabled`` defaults FALSE, and a deployment that never
    enabled it is how ~430 rows were stranded on 2026-07-27. The repair must be
    queued per child and must target the CHILD — a backfill logged against the
    parent repairs nothing.
    """
    run = await _run(embed_raises=True)
    assert "embed_or_publish" in run.labels
    scheduled = run.scheduled_for("embed_or_publish")
    assert len(scheduled) == len(CHUNKS), (
        f"expected one re-embed per child, got {len(scheduled)}: {scheduled}"
    )
    assert run.parent_id not in scheduled, "re-embed was queued against the parent"


async def test_the_repair_pairs_each_child_id_with_its_own_content() -> None:
    """The payload-to-result join is by ``client_request_id``, not by position.

    A mispaired entry does not merely lose a repair. It embeds THIS child's
    text and persists the vector against ANOTHER child's row, so that row's
    embedding silently stops matching its own stored content — a recall-quality
    corruption with no error and no log, on a path that only runs when the
    provider is already degraded.

    Storage does guarantee input order today: ``memory_add_all`` collects
    ``RETURNING`` into a dict and builds its response by iterating the input.
    So this is a contract-drift probe, not a reproduction of live behaviour —
    it fails if the join ever regresses to ``zip``.
    """
    run = await _run(embed_raises=True, rotate_results=True)
    assert run.raised is None
    assert len(run.children) == len(CHUNKS), "no children written — test is vacuous"

    expected = {
        run.assigned_ids[c["client_request_id"]]: (c["content"], c["content_hash"])
        for c in run.children
    }
    child_ids = set(expected)
    actual = {
        mid: (content, chash)
        for mid, content, chash in run.reembeds
        if mid in child_ids
    }
    assert len(actual) == len(CHUNKS), f"expected one repair per child, got {actual}"
    assert actual == expected, (
        "a child's re-embed was queued against another child's row"
    )


async def test_a_duplicate_refusal_reports_no_unrepairable_rows(caplog) -> None:
    """Nothing was written, so nothing may be reported as written.

    A batch refused by migration 040 persists NO children. Every payload still
    carries ``embedding=None`` from the degraded embed, so without the
    ``None``-vs-``[]`` distinction each one falls through to the no-id branch
    and claims a row was "persisted unembedded" with "NO re-embed scheduled" —
    N false alarms for rows that never reached the table.

    That matters because of WHEN it fires. Embed degraded plus children already
    present is the retry-after-degradation case this fix exists for, so the
    false alarms would land in the middle of the incident, pointing an on-call
    engineer at orphaned rows that do not exist.

    A regression of my own making: the positional ``zip`` this replaced paired
    against an empty list and iterated zero times, so the join is what exposed
    this branch.
    """
    with caplog.at_level("DEBUG"):
        run = await _run(embed_raises=True, duplicate_refusal=True)

    assert run.raised is None
    assert run.reembeds == [], "a repair was queued for a row that was never written"

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [], (
        "a duplicate refusal is a fully-explained WARNING-level outcome; "
        f"it must not raise ERRORs: {[r.getMessage()[:90] for r in errors]}"
    )
    # Anti-vacuity: the run must actually have reached the refusal, and the
    # cause must still be on the record. Asserting only the absence above would
    # pass just as well if the handler had never got that far.
    assert any("refused as duplicates" in r.getMessage() for r in caplog.records), (
        f"the refusal itself went unlogged: {[r.getMessage()[:60] for r in caplog.records]}"
    )


async def test_an_unparseable_child_id_does_not_escape_the_request(caplog) -> None:
    """The repair step must not become the thing that raises after the commit.

    ``UUID(str(child_id))`` runs after the parent AND the children are
    committed, and outside the request's ``try/finally``. An id that stopped
    being a UUID string would answer a fully-persisted write with a 500 — the
    exact H-09 shape this PR removes, moved from the embed step to the repair
    step.

    The guard is PER CHILD, and the second assertion is what proves it: a catch
    around the whole loop would swallow the error just as well while silently
    cancelling every other child's repair, turning one unrepaired row into N.
    """
    with caplog.at_level("ERROR"):
        run = await _run(embed_raises=True, bad_id_index=0)

    assert run.raised is None, f"the repair step escaped the request: {run.raised!r}"
    # The other two children still get theirs — the guard skipped one child, not
    # the sweep.
    assert len(run.reembeds) == len(CHUNKS) - 1, (
        f"expected the surviving children to still be repaired, got {run.reembeds}"
    )
    assert any("could not be scheduled" in r.getMessage() for r in caplog.records), (
        "the unrepairable child was dropped silently"
    )


async def test_the_degrade_warning_does_not_promise_a_repair_it_cannot_make(
    caplog,
) -> None:
    """The embed-degrade warning fires BEFORE the insert, so it cannot know.

    TWO claims, and both had to become conditional. Whether a repair is
    scheduled is settled two steps later; whether the children are persisted at
    all is settled one step later, and a duplicate refusal writes none of them.
    This run is that case, so in these logs the warning sits directly above
    "children refused as duplicates" — an unconditional wording would read as
    contradicting the line beneath it, during exactly the incident this fix is
    for.

    The first draft made only the repair clause conditional and left
    "persisting" asserting the same thing one clause earlier, which is why both
    are pinned here.
    """
    with caplog.at_level("WARNING"):
        run = await _run(embed_raises=True, duplicate_refusal=True)

    # Anti-vacuity: the refusal must actually have happened, or the warning
    # under test was never a premature claim about anything. NOT
    # ``run.children == []`` — that field holds the payloads SUBMITTED, which
    # exist either way; the refusal is what decides whether any were written.
    assert any("refused as duplicates" in r.getMessage() for r in caplog.records), (
        "the run never reached the refusal, so this proves nothing"
    )
    assert run.reembeds == [], "a repair was queued despite nothing being written"
    degrade = [
        r.getMessage() for r in caplog.records if "child embed failed" in r.getMessage()
    ]
    assert degrade, "the embed degrade went unlogged"
    assert not any("unembedded with a re-embed queued" in m for m in degrade), (
        "the warning asserts a repair that this run never queued"
    )
    assert not any("persisting" in m for m in degrade), (
        f"the warning asserts a write that this run never made: {degrade}"
    )


# ---------------------------------------------------------------------------
# Over-refusal / no-op guards
# ---------------------------------------------------------------------------


async def test_the_healthy_path_schedules_no_child_re_embeds() -> None:
    """OVER-REFUSAL GUARD. A working provider must cost nothing.

    The children carry vectors, so the repair loop must find nothing to do —
    otherwise every ordinary auto-chunk write would queue N pointless tasks.
    """
    run = await _run(embed_raises=False)
    assert run.raised is None
    assert len(run.children) == len(CHUNKS)
    assert all(c["embedding"] is not None for c in run.children)
    assert all("embedding_pending" not in c["metadata_"] for c in run.children)
    assert run.scheduled_for("embed_or_publish") == set()


async def test_a_child_with_no_returned_id_is_logged_not_silently_dropped(
    caplog,
) -> None:
    """The one case that cannot be repaired must be loud.

    A row that persisted unembedded with no queued repair is strictly worse
    than the counted case, and only the nightly sweep — off by default — would
    ever find it. It must not pass quietly.
    """
    with caplog.at_level("ERROR"):
        run = await _run(embed_raises=True, return_ids=False)
    assert run.raised is None
    assert run.scheduled_for("embed_or_publish") == set()
    assert any(
        "no usable id" in r.message or "no usable id" in r.getMessage()
        for r in caplog.records
    ), (
        f"expected a loud no-id error; got {[r.getMessage()[:80] for r in caplog.records]}"
    )


async def test_the_legacy_handler_degrades_too(caplog) -> None:
    """The rollback path carries the same fix, driven through the real handler.

    ``_create_memory_legacy`` had the identical shape and I first left it alone
    on the grounds that it was dead behind ``_USE_PIPELINE_WRITE=True``. That
    was wrong: the flag's own comment documents flipping it as the
    emergency-rollback lever, so the path is DORMANT, not dead. And the
    correlation is adverse — an emergency rollback is plausibly happening
    BECAUSE something is degraded, which is the same condition that trips this
    bug. The defect would have resurfaced during exactly the incident the lever
    exists for.

    Driven with the flag flipped, so this exercises the dispatch too rather
    than calling the private handler directly.
    """
    run = await _run(embed_raises=True, legacy=True)
    assert run.raised is None, (
        f"legacy child embed failure escaped as {type(run.raised).__name__}: {run.raised}"
    )
    assert len(run.children) == len(CHUNKS), "legacy path wrote no children"
    assert all(c["embedding"] is None for c in run.children)
    assert all(c["metadata_"].get("embedding_pending") is True for c in run.children)
    assert len(run.scheduled_for("embed_or_publish")) == len(CHUNKS)


async def test_the_legacy_healthy_path_schedules_nothing() -> None:
    """OVER-REFUSAL GUARD for the rollback path, same as the pipeline one."""
    run = await _run(embed_raises=False, legacy=True)
    assert run.raised is None
    assert all(c["embedding"] is not None for c in run.children)
    assert run.scheduled_for("embed_or_publish") == set()


async def test_both_paths_share_one_degrade_policy() -> None:
    """Anti-drift: the two handlers must not grow separate copies.

    H-09 existed in two places at once because the auto-chunk logic was
    written twice. The embed-degrade, the pending flag and the repair
    scheduling now live in one helper each; this pins that both handlers route
    through them, so a change to one cannot silently miss the other.
    """
    import inspect

    src = inspect.getsource(memory_service)
    # The raw call must appear nowhere outside the shared helper.
    raw = src.count("await get_embeddings_batch(child_texts")
    assert raw == 1, (
        f"expected the child batch embed to be called in exactly one place "
        f"(the shared helper); found {raw}"
    )
    assert src.count("_embed_children_or_degrade(") >= 3, (
        "both handlers must call the helper"
    )
    assert src.count("_queue_child_reembeds(") >= 3, "both handlers must queue repairs"
    assert src.count("_mark_child_embedding_pending(") >= 3, (
        "both handlers must mark pending"
    )


async def test_the_no_id_log_carries_no_memory_content(caplog) -> None:
    """The log names the response SHAPE, never the response.

    The bulk result belongs to a row carrying the fact text; interpolating it
    would put memory content, and any PII in it, into an ERROR log.
    """
    with caplog.at_level("ERROR"):
        await _run(embed_raises=True, return_ids=False)
    joined = " ".join(r.getMessage() for r in caplog.records)
    for chunk in CHUNKS:
        assert chunk not in joined, f"memory content leaked into a log: {chunk!r}"


# ---------------------------------------------------------------------------
# The two markers H-10's governance cascade reads
# ---------------------------------------------------------------------------


async def test_the_auto_chunk_write_records_the_parent_child_link() -> None:
    """Pins the contract ``governance_remediation`` depends on (H-10).

    On a deferred deployment these children are committed before any governance
    verdict exists, so the verdict — which arrives later naming only the parent
    — has to find them afterwards. It does that with exactly two things written
    here:

    * ``auto_chunked`` on the PARENT, which gates the lookup. Gating keeps a
      JSON-key query with no supporting index off every ordinary drop, and a
      compliance tenant configured ``drop`` remediates constantly. If this
      marker ever stops being written, the cascade silently stops running and
      the leak returns with no test failing anywhere near it — which is why the
      assertion lives here, beside the write, rather than only in the
      remediation tests where it would be a stub asserting itself.
    * ``parent_memory_id`` on each CHILD, which is what the lookup matches on.

    Deliberately asserted on the payloads sent to storage, not on a return
    value: what the row carries is what a later remediation can read.
    """
    run = await _run(embed_raises=False)

    assert run.parent_payload, "no parent was written — the test is vacuous"
    parent_meta = run.parent_payload.get("metadata_") or {}
    assert parent_meta.get("auto_chunked") is True, parent_meta
    assert parent_meta.get("child_count") == len(CHUNKS), parent_meta

    assert len(run.children) == len(CHUNKS)
    for child in run.children:
        assert (child.get("metadata_") or {}).get("parent_memory_id") == run.parent_id
