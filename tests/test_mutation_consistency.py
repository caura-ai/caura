"""A mutation has to finish what it starts.

Nine findings, one shape: an update changed the thing it was asked to change
and left everything DERIVED from it describing the old value. A re-embed that
failed left the row unsearchable with nothing scheduled and nothing said. An
edit that broke a supersession left the superseded row outdated forever. Content
replaced wholesale kept the entity graph mined out of the text it replaced. A
deleted document left its minted memory recallable.

The read-replica cases are the same defect one layer down: the mutation
committed to the primary and the very next read — the one that decides whether
the row exists, or builds the response, or feeds a read-modify-write — went to a
replica that had not caught up. What comes back is not merely stale; it
contradicts the write that just succeeded.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# oss-0902-l-39 — read-your-write must not go to the replica
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Captures how each storage call was routed and what it was sent."""

    def __init__(self, mem: dict):
        self._mem = mem
        self.get_memory_reads: list[bool] = []
        self.status_updates: list[tuple] = []
        self.reset_calls: list[tuple] = []
        self.patches: list[dict] = []
        self.dup: dict | None = None
        self.dup_args: dict = {}
        self.link_writes: list[list] = []
        self.reset_raises = False
        self.links_refused = False

    async def get_memory(self, memory_id, tenant_id, *, read=True):
        self.get_memory_reads.append(read)
        return dict(self._mem)

    async def update_memory(self, memory_id, tenant_id, patch):
        self.patches.append(patch)
        return dict(self._mem)

    async def update_memory_status(self, memory_id, status, *, tenant_id, **kw):
        self.status_updates.append((memory_id, status))
        return {}

    async def reset_entity_artifacts(self, tenant_id, memory_id):
        self.reset_calls.append((tenant_id, memory_id))
        if self.reset_raises:
            raise RuntimeError("storage down")
        return {"links": 0, "relations": 0, "entities": 0}

    async def find_duplicate_hash(
        self, tenant_id, content_hash, exclude_id=None, fleet_id=None, agent_id=None
    ):
        self.dup_args = {
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "exclude_id": exclude_id,
        }
        return self.dup

    async def update_memory_entities(self, memory_id, tenant_id, links):
        self.link_writes.append(list(links))
        # ``None`` is what ``_patch`` returns when storage refuses an entity the
        # tenant does not own; the route turns it into a 422.
        return None if self.links_refused else {"ok": True}

    async def get_entity_links_for_memories(self, ids, tenant_id):
        return {}


async def test_update_reads_its_own_write_from_the_primary(monkeypatch):
    """Both reads in ``update_memory`` are read-your-write.

    The first decides whether the row EXISTS — from a replica it 404s a memory
    created moments ago, which a client cannot tell from "you deleted it". The
    second builds the PATCH RESPONSE, so a lagging replica echoes the caller its
    own pre-edit content as the result of a successful edit.
    """
    client = await _run_update(monkeypatch, content="a new body for this memory")

    assert client.get_memory_reads == [False, False], (
        f"expected both reads on the writer, got read flags {client.get_memory_reads!r}"
    )


# ---------------------------------------------------------------------------
# oss-0902-m-51 — a broken supersession must release the row it superseded
# ---------------------------------------------------------------------------


async def test_editing_a_superseding_memory_revives_the_superseded_row(monkeypatch):
    """Clearing our pointer is only half a retraction.

    The other row was set ``outdated`` BECAUSE this one superseded it. Dropping
    the edge and walking away left it outdated with nothing superseding it, and
    the detector never revisits a row whose conflict is gone.
    """
    superseded_id = str(uuid4())
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        mem_extra={"supersedes_id": superseded_id},
        superseded_status="outdated",
    )

    assert (superseded_id, "active") in client.status_updates, (
        f"superseded row never revived; status calls were {client.status_updates!r}"
    )
    assert client.patches and client.patches[0].get("supersedes_id") is None


async def test_a_rejected_edit_does_not_revive_the_superseded_row(monkeypatch):
    """Building a patch must not have side effects.

    The revert is a live PATCH against ANOTHER row, and two checks AFTER it can
    still abort the request — ``metadata`` null-in-merge-mode (400) and an
    ``entity_links`` entity the tenant does not own (422). Running it inline
    left the superseded row flipped back to ``active`` while the edit that was
    supposed to justify reviving it was rejected, with nothing to roll it back:
    a row that should still read ``outdated`` resurfacing in recall.
    """
    from fastapi import HTTPException

    from core_api.schemas import EntityLinkIn

    superseded = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        await _run_update(
            monkeypatch,
            content="a new body for this memory",
            entity_links=[EntityLinkIn(entity_id=uuid4(), role="subject")],
            links_refused=True,
            mem_extra={"supersedes_id": superseded},
        )

    assert exc.value.status_code == 422
    # The client the helper built is gone with the exception, so assert on the
    # only durable evidence: nothing was written to the superseded row.
    assert not _LAST_CLIENT.status_updates, (
        f"a rejected edit revived the row it superseded: {_LAST_CLIENT.status_updates!r}"
    )


async def test_a_superseded_row_with_another_status_is_left_alone(monkeypatch):
    """The over-correction guard, taken from Path-C retraction.

    A row that is ``archived`` is outdated for reasons that have nothing to do
    with this supersession. Reviving it would be a second bug wearing the first
    one's fix.
    """
    superseded_id = str(uuid4())
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        mem_extra={"supersedes_id": superseded_id},
        superseded_status="archived",
    )

    assert client.status_updates == [], (
        f"a non-supersession status was overwritten: {client.status_updates!r}"
    )


# ---------------------------------------------------------------------------
# oss-0902-m-53 — the entity graph describes the content that is there now
# ---------------------------------------------------------------------------


async def test_content_change_clears_the_old_entity_graph(monkeypatch):
    """Extraction only ever ADDS, so nothing removed the old content's links.

    A row edited from "Alice joined Acme" to "Bob joined Globex" kept Alice and
    Acme — linked, related, and still ranking the row in recall for names its
    content no longer contains.
    """
    client = await _run_update(monkeypatch, content="a new body for this memory")

    assert len(client.reset_calls) == 1, "the stale graph rows were not cleared"
    assert client.patches[0].get("subject_entity_id") is None, (
        "subject_entity_id still names the entity the OLD content was about"
    )


async def test_a_metadata_only_edit_leaves_the_graph_alone(monkeypatch):
    """The guard against over-correcting: no content change, no graph reset.

    Clearing links on every PATCH would discard a correct graph because someone
    renamed a title.
    """
    client = await _run_update(monkeypatch, title="just a new title")

    assert client.reset_calls == [], "an unrelated edit destroyed the entity graph"


async def test_links_the_request_named_are_written_once(monkeypatch):
    """A PATCH carrying both ``content`` and ``entity_links`` writes them ONCE.

    The add runs before the storage PATCH and the reset after it, so these links
    exist while the reset fires. They survive because storage spares
    ``source='caller'`` rows — provenance, not a re-apply, which is what lets
    this stay a single round trip. An earlier revision of this fix re-asserted
    the set after the reset; that worked but could not protect a link curated in
    an EARLIER request, which is the case that actually matters and which
    ``core-storage-api/tests/test_h02_purge_entity_artifacts.py`` now pins.
    """
    from core_api.schemas import EntityLinkIn

    entity_id = uuid4()
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        entity_links=[EntityLinkIn(entity_id=entity_id, role="subject")],
    )

    assert len(client.reset_calls) == 1, "the stale graph rows were not cleared"
    assert client.link_writes == [[{"entity_id": str(entity_id), "role": "subject"}]], (
        f"the caller's links were not written exactly once: {client.link_writes!r}"
    )


async def test_a_failed_graph_reset_does_not_fail_the_edit(monkeypatch):
    """Everything here runs AFTER the content patch committed.

    So propagating hands the caller a 500 for an edit that landed — and a retry
    repairs nothing, because the row's content now equals what was sent, making
    ``content_changed`` False and skipping this whole branch forever. That would
    also lose the three background tasks scheduled below it and nowhere else.
    ``_revert_superseded_row`` already takes this position; the two had no
    business disagreeing four lines apart.
    """
    scheduled: list = []
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        reset_raises=True,
        scheduled=scheduled,
    )

    assert client.reset_calls, "the reset was never attempted"
    assert "contradiction_detection" in scheduled, (
        f"a failed reset swallowed the tasks sequenced after it: {scheduled!r}"
    )


async def test_an_explicit_subject_beats_the_content_auto_clear(monkeypatch):
    """A subject the caller NAMES survives a content edit that would clear it.

    The content-change branch writes ``subject_entity_id = None`` before
    ``simple_fields`` runs, so the ordering is what makes this work — and it
    works even when the caller re-asserts the value the row already has, because
    storage serialises UUIDs to ``str`` (``orm_to_dict``) while ``MemoryUpdate``
    parses them to ``UUID``. The loop's ``old_val != new_val`` is therefore true
    for any named subject, so the clear can never be the last write. Pinned
    because a review read this as a live bug: it is not reachable, but it is
    only unreachable for a reason worth recording.
    """
    subject = uuid4()
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        subject_entity_id=subject,
        mem_extra={"subject_entity_id": str(subject)},
    )

    assert client.patches[0].get("subject_entity_id") == subject, (
        f"an explicitly named subject_entity_id did not land: {client.patches[0]!r}"
    )


async def test_a_content_edit_naming_no_links_writes_none(monkeypatch):
    """The guard on the re-apply: it must not invent a write of its own.

    An unconditional re-apply would turn every content edit into an extra
    storage round trip with an empty payload.
    """
    client = await _run_update(monkeypatch, content="a new body for this memory")

    assert client.link_writes == [], (
        f"an empty link write was issued: {client.link_writes!r}"
    )


# ---------------------------------------------------------------------------
# oss-0902-m-50 — a failed re-embed must say so and schedule the repair
# ---------------------------------------------------------------------------


async def test_a_failed_reembed_is_flagged_and_rescheduled(monkeypatch):
    """Unlike every create path, the update path did neither.

    ``embedding_pending`` is public API — ``MemoryOut.metadata`` documents its
    ABSENCE as "that stage ran inline" — so a PATCH that omits it states the
    row is embedded when it is not, and no backfill was queued either.
    """
    scheduled: list = []
    monkeypatch.setattr(
        "core_api.services.memory_service._schedule_embed_or_reembed",
        lambda *a, **k: scheduled.append(a) or _noop(),
    )
    client = await _run_update(
        monkeypatch, content="a new body for this memory", embedding=None
    )

    patch = client.patches[0]
    assert patch.get("embedding") is None, (
        "a failed embed must persist NULL, not the old vector"
    )
    meta = patch.get("metadata_patch") or patch.get("metadata_") or {}
    assert meta.get("embedding_pending") is True, (
        f"the caller is not told the row is unsearchable: {meta!r}"
    )
    assert scheduled, "no backfill scheduled; the row waits for the nightly sweep"


async def test_a_successful_reembed_clears_the_pending_flag(monkeypatch):
    """The other direction, and it has to be a WRITE, not merely an omission.

    ``embedding_pending`` is public API — ``MemoryOut.metadata`` documents its
    absence as "that stage ran inline" — so a row that failed to embed once,
    here or at create time, kept reporting ``True`` forever once a later edit
    re-embedded it successfully. The async worker clears the flag on its own
    success, but a successful INLINE re-embed schedules no worker task, so
    nothing else was ever going to.

    Both homes, because the C25 read view gives ``_system`` precedence:
    clearing only the legacy top-level key would leave the row still reporting
    pending. ``set_system_value`` writes both.
    """
    client = await _run_update(
        monkeypatch, content="a new body for this memory", embedding=[0.1, 0.2, 0.3]
    )

    patch = client.patches[0]
    meta = patch.get("metadata_patch") or patch.get("metadata_") or {}
    assert meta.get("embedding_pending") is False, (
        f"a successful re-embed left a stale pending flag unrepaired: {meta!r}"
    )
    assert meta.get("_system", {}).get("embedding_pending") is False, (
        f"the _system home, which the read view prefers, was not cleared: {meta!r}"
    )


# ---------------------------------------------------------------------------
# oss-0814-m-04 — the update dedup must key on what the unique index keys on
# ---------------------------------------------------------------------------


async def test_update_dedup_is_scoped_to_the_rows_fleet(monkeypatch):
    """Storage defaults the lookup to the NULL/empty fleet group.

    Omitting ``fleet_id`` therefore did not mean "any fleet", it meant "only the
    fleetless rows" — so for a fleet-scoped memory the gate could never match
    and was dead code for every tenant that uses fleets.
    """
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        mem_extra={"fleet_id": "fleet-1"},
    )

    assert client.dup_args.get("fleet_id") == "fleet-1", (
        f"dedup searched the wrong fleet group: {client.dup_args!r}"
    )


async def test_update_dedup_is_scoped_to_the_rows_own_agent(monkeypatch):
    """The over-fix guard, and the half that fixing the fleet alone exposes.

    ``uq_memories_live_content_hash`` keys on
    ``(tenant, COALESCE(fleet,''), agent, content_hash)``. Passing the fleet and
    not the agent turns a gate that could never fire into one that fires too
    WIDE — refusing an edit whose content matches ANOTHER agent's row in the
    same fleet, which the index admits deliberately (CAURA-721: two agents
    recording identical content are two independent observations).

    ``tests/test_caura721_dedup_agent_scope.py`` pins the same rule end to end
    through the route; this pins the argument actually reaching storage, which
    is where it went missing.
    """
    client = await _run_update(
        monkeypatch,
        content="a new body for this memory",
        mem_extra={"fleet_id": "fleet-1", "agent_id": "agent-a"},
    )

    assert client.dup_args.get("agent_id") == "agent-a", (
        f"dedup was not pinned to the edited row's owner: {client.dup_args!r}"
    )


async def test_the_duplicate_409_names_the_row_it_found(monkeypatch):
    """The endpoint answers ``{"memory_id": ...}``; the caller read ``id``.

    So on the one path where the gate could fire, the 409 reported
    ``existing_id: null`` and a message naming no row at all.
    """
    from fastapi import HTTPException

    existing = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        await _run_update(
            monkeypatch,
            content="a new body for this memory",
            dup={"memory_id": existing},
        )

    assert exc.value.status_code == 409
    detail = str(exc.value.detail)
    assert existing in detail, f"the 409 does not name the existing row: {detail}"
    assert "None" not in detail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# The client from the most recent ``_run_update``. Needed because a run that
# RAISES never returns one, and the interesting assertion for those is what was
# written before the raise.
_LAST_CLIENT: _RecordingClient = None  # type: ignore[assignment]


async def _noop():
    return None


async def _run_update(
    monkeypatch,
    *,
    content: str | None = None,
    title: str | None = None,
    embedding=(0.1, 0.2),
    mem_extra: dict | None = None,
    superseded_status: str = "outdated",
    dup: dict | None = None,
    entity_links=None,
    subject_entity_id=None,
    reset_raises: bool = False,
    links_refused: bool = False,
    scheduled: list | None = None,
):
    """Drive ``update_memory`` against a recording client and return it."""
    from core_api.schemas import MemoryUpdate
    from core_api.services import memory_service

    memory_id = uuid4()
    mem = {
        "id": str(memory_id),
        "tenant_id": "t1",
        "fleet_id": None,
        "agent_id": "a1",
        "content": "the original body of this memory",
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "subject_entity_id": str(uuid4()),
        "metadata_": {},
        # MemoryOut requires these; the update path echoes the row back through it.
        "weight": 0.5,
        "recall_count": 0,
        "created_at": "2026-09-17T00:00:00+00:00",
        "updated_at": "2026-09-17T00:00:00+00:00",
    }
    mem.update(mem_extra or {})

    global _LAST_CLIENT
    client = _RecordingClient(mem)
    _LAST_CLIENT = client
    client.dup = dup
    client.reset_raises = reset_raises
    client.links_refused = links_refused

    # The superseded row is fetched by id; give it the status under test.
    real_get = client.get_memory

    async def _get(memory_id_arg, tenant_id, *, read=True):
        if mem.get("supersedes_id") and str(memory_id_arg) == str(mem["supersedes_id"]):
            return {"id": str(mem["supersedes_id"]), "status": superseded_status}
        return await real_get(memory_id_arg, tenant_id, read=read)

    client.get_memory = _get  # type: ignore[method-assign]

    monkeypatch.setattr(memory_service, "get_storage_client", lambda: client)

    async def _embed(*a, **k):
        return list(embedding) if embedding is not None else None

    monkeypatch.setattr(memory_service, "get_embedding", _embed)

    class _Config:
        semantic_dedup_enabled = False
        entity_extraction_enabled = False

    async def _resolve_config(_tenant):
        return _Config()

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )

    # ``close_scheduled_coro`` rather than a local closer: conftest's version
    # walks the nested coroutine tree, where an inline one closes only the
    # outermost and leaves the inner ``tracked_task(...)`` argument unstarted —
    # trading one dropped coroutine for another. Four other suites already
    # import it.
    monkeypatch.setattr(
        memory_service, "track_task", lambda coro, *a, **k: close_scheduled_coro(coro)
    )

    def _tracked(coro, *a, **k):
        # The task NAME is an argument to ``tracked_task``, not to
        # ``track_task`` — recording it on the wrong one collects only ``None``.
        if scheduled is not None and a:
            scheduled.append(a[0])
        return coro

    monkeypatch.setattr(memory_service, "tracked_task", _tracked)

    fields = {}
    if content is not None:
        fields["content"] = content
    if title is not None:
        fields["title"] = title
    if entity_links is not None:
        fields["entity_links"] = entity_links
    if subject_entity_id is not None:
        fields["subject_entity_id"] = subject_entity_id
    await memory_service.update_memory(memory_id, "t1", MemoryUpdate(**fields))
    return client


# ---------------------------------------------------------------------------
# oss-0902-m-24 — deleting a document removes what it minted
# ---------------------------------------------------------------------------


async def test_unminting_matches_the_provenance_the_mint_wrote(monkeypatch):
    """The doc write mints a memory so the content is recallable.

    Deleting the document removed the document and left that copy behind,
    searchable forever, with nothing in the product able to reach it — the doc
    it was provenance for no longer exists to be deleted again.

    The filter is asserted against ``doc_provenance`` rather than a literal:
    re-typing the two key names at the matching end is exactly how this stops
    matching without anything failing.
    """
    from core_api.clients import storage_client as sc_mod
    from core_api.services.doc_indexing import doc_provenance
    from core_api.services.doc_memory import safe_unmint_doc_memory

    filters: list[dict] = []

    class _SC:
        async def soft_delete_by_filter(self, data):
            filters.append(data)
            return 1

    monkeypatch.setattr(sc_mod, "get_storage_client", lambda: _SC())

    assert await safe_unmint_doc_memory("notes", "d1", tenant_id="t1") == 1
    assert filters[0]["tenant_id"] == "t1"
    assert filters[0]["metadata_filter"] == doc_provenance("notes", "d1")


async def test_a_failed_unmint_does_not_fail_the_document_delete(monkeypatch):
    """The document IS deleted; that is what the caller asked for.

    Turning a successful delete into a 5xx because the derived row survived
    invites a retry that 404s on the document and never revisits the memory.
    """
    from core_api.clients import storage_client as sc_mod
    from core_api.services.doc_memory import safe_unmint_doc_memory

    class _SC:
        async def soft_delete_by_filter(self, data):
            raise RuntimeError("storage down")

    monkeypatch.setattr(sc_mod, "get_storage_client", lambda: _SC())

    assert await safe_unmint_doc_memory("notes", "d1", tenant_id="t1") == 0


async def test_both_delete_doors_un_mint(monkeypatch):
    """The mint has two entry points, so the delete needs two.

    The REST route had the un-mint and MCP ``caura_doc op=delete`` did not, so
    the same document deleted through the other door left its minted memory
    recallable — the defect, surviving at the sibling call site.
    """
    import inspect

    from core_api import mcp_server
    from core_api.auth import AuthContext
    from core_api.routes import documents as docs
    from core_api.services import doc_memory

    calls: list[tuple] = []

    async def _unmint(collection, doc_id, *, tenant_id):
        calls.append((collection, doc_id, tenant_id))
        return 1

    monkeypatch.setattr(doc_memory, "safe_unmint_doc_memory", _unmint)

    class _SC:
        async def delete_document(self, *args, **kw):
            return True

    monkeypatch.setattr(docs, "get_storage_client", lambda: _SC())
    monkeypatch.setattr(docs, "log_action", lambda **kw: _noop())

    await docs.delete_document(
        doc_id="d1",
        tenant_id="t1",
        collection="notes",
        auth=AuthContext(tenant_id="t1"),
    )
    assert calls == [("notes", "d1", "t1")], "the REST delete stopped un-minting"

    # The MCP door, asserted through the source rather than by driving the tool
    # (whose handler needs the whole auth/settings stack stood up). What can
    # regress here is the CALL going missing, and that is what this sees.
    assert "safe_unmint_doc_memory" in inspect.getsource(mcp_server), (
        "MCP caura_doc delete stopped un-minting"
    )


# ---------------------------------------------------------------------------
# oss-0902-m-33 / m-32 — the inbox binds against the right doc, from the writer
# ---------------------------------------------------------------------------


async def test_inbox_resolves_the_update_target_by_slug():
    """``target`` carries ``target_content_hash`` and nothing else.

    Reading ``target["slug"]`` therefore always yielded None, the binding gate
    got no live doc, and every ``kind='update'`` edit 404'd claiming no live
    skill exists — for a skill that does. The second case is the one that was
    broken: a ``slug`` inside ``target`` is not where the slug lives, and a
    resolver that honoured it would still be reading a field nothing writes.
    """
    from core_api.routes.skills_inbox import _binding_target_slug

    real = {
        "kind": "update",
        "slug": "my-skill",
        "target": {"target_content_hash": "h"},
    }
    assert _binding_target_slug(real) == "my-skill"

    old_shape = {
        "kind": "update",
        "target": {"slug": "my-skill", "target_content_hash": "h"},
    }
    assert _binding_target_slug(old_shape) is None

    assert _binding_target_slug({"kind": "create", "slug": "my-skill"}) is None


async def test_the_inbox_loader_reads_the_writer(monkeypatch):
    """Every caller of ``_load_doc_or_404`` is a read-modify-write.

    From a replica, approve reloads the version from before a just-saved edit
    and writes it back — a lost update the TOCTOU re-check cannot catch,
    because it re-reads the same stale copy and agrees with itself.
    """
    from core_api.routes import skills_inbox

    reads: list[bool] = []

    class _SC:
        async def get_document(self, *, tenant_id, collection, doc_id, read=True, **kw):
            reads.append(read)
            return {"slug": doc_id}

    monkeypatch.setattr(skills_inbox, "get_storage_client", lambda: _SC())

    await skills_inbox._load_doc_or_404(tenant_id="t1", slug="my-skill")

    assert reads == [False], (
        f"the inbox loaded a read-modify-write from the replica: {reads!r}"
    )


# ---------------------------------------------------------------------------
# oss-0814-l-03 — a settings write must not be undone by a stale re-cache
# ---------------------------------------------------------------------------


async def test_a_settings_miss_reads_the_primary(monkeypatch):
    """The reload after an invalidation is the dangerous one.

    Served by the replica it re-caches the PRE-update settings for the full
    5-minute TTL, so a write meant to tighten a control appears to land and
    does not take effect — hidden for exactly as long as the cache was meant to
    help.
    """
    from core_api.services import organization_settings as os_mod

    reads: list[bool] = []

    from core_api.clients.storage_client import CoreStorageClient

    class _Client(CoreStorageClient):
        def __init__(self):  # bypass CoreStorageClient.__init__, which builds a pool
            pass

        async def _get(self, path, *, read=True, **params):
            reads.append(read)
            return {"settings": {"k": "v"}}

    # Driven through the REAL ``get_org_settings`` rather than a fake of it:
    # the routing decision moved INTO the client (the kwarg had one caller and
    # one correct value), so a fake with its own ``read`` parameter would be
    # asserting a seam that no longer exists.
    monkeypatch.setattr(os_mod, "get_storage_client", lambda: _Client())
    os_mod._settings_cache.pop("t1", None)

    assert await os_mod._load_and_cache("t1") == {"k": "v"}
    assert reads == [False], f"the settings miss went to the replica: {reads!r}"


async def test_the_settings_write_still_invalidates():
    """Priming the cache post-write instead of invalidating is not equivalent.

    ``subscribe(broadcast=True)`` gives every process its own subscription, the
    publisher included, so the writer receives its own ``SETTINGS_CHANGED`` and
    evicts whatever it just primed. The reload happens either way — which is why
    the fix is the ``read=False`` above and not a prime, and why this pins that
    the invalidation stayed.
    """
    import inspect

    from core_api.services.organization_settings import update_settings

    assert "invalidate_cache(tenant_id)" in inspect.getsource(update_settings)
