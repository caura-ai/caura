"""Background worker: extract entities from a memory and upsert them."""

import asyncio
import logging
import re
from typing import Any, Literal
from uuid import UUID

from common.embedding import get_embedding
from common.entity_naming import canonical_match_key
from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    CROSS_LINK_MEMORY_BATCH_SIZE,
    CROSS_LINK_SIMILARITY_THRESHOLD,
    CROSS_LINK_TEXT_VERIFY,
    ENTITY_NAME_BLOCKLIST,
    ENTITY_RESOLUTION_THRESHOLD,
    MIN_ENTITY_NAME_LENGTH,
)
from core_api.schemas import RelationUpsert
from core_api.services.audit_service import log_action
from core_api.services.entity_extraction import extract_entities_from_content
from core_api.services.entity_service import upsert_relation

logger = logging.getLogger(__name__)


# CAURA graph-build fix (A): reject literal VALUES and attribute/field NAMES so they
# never become entity nodes (and thus hub bridges that explode entity_lookup's pool).
# Shapes only — preserves legit named identifiers like "PR-2025-A" / "gpt-5.4-nano"
# (no underscores, contain letters) while dropping dates, numbers/money/percent, and
# snake_case field names (sla_uptime, q3_revenue, founded_year).
_LITERAL_OR_ATTR_RE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}"  # ISO date: 2024-03-23
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"  # slashed/dashed date
    r"|\$?\d[\d,]*(?:\.\d+)?\s*%?\s*[kmb]?"  # number / money / percent: 14402, $35.4M, 95.1%, 1935
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"  # snake_case field/attribute name
    r")$",
    re.IGNORECASE,
)


# A42 — parenthetical qualifiers, e.g. the "(delaware)" in "acme (delaware)".
# Matches the discriminator vocabulary ``_reattach_subject_discriminators``
# already established ("#NNNN" and parentheticals); deliberately NOT any
# trailing word, because unbracketed tokens are usually harmless surface
# variation ("acme" / "acme corp") rather than a distinguisher.
_QUALIFIER_RE = re.compile(r"[(\[]([^)\]]{1,64})[)\]]")


def _qualifier_signature(name: str) -> frozenset[str]:
    """Bracketed qualifiers in ``name``, normalised for comparison."""
    return frozenset(" ".join(m.strip().lower().split()) for m in _QUALIFIER_RE.findall(name) if m.strip())


def _same_identifier_signature(a: str, b: str) -> bool:
    """Two names may only merge if nothing in them says they are different things.

    CAURA graph-build fix (B): same set of digit-bearing identifier tokens.
    Synthetic suffix-distinct names like 'comet #0002' vs 'comet #0012' embed
    near-identically and trip the 0.85 similarity merge, collapsing distinct
    entities into one contaminated mega-node.

    A42 (A33 mechanism ②): digits alone are too narrow. Two genuinely distinct
    entities distinguished by a NON-digit qualifier — 'acme (delaware)' vs
    'acme (ohio)' — both yield an empty digit set, compare equal, and merge.
    Downstream that reads as same_subject=true and produces a false
    contradiction between two different things.

    Asymmetry is deliberate: a name with NO qualifier merges freely with a
    qualified one ('acme' vs 'acme (ohio)' -> allowed). An absent qualifier
    means "unspecified", not "different", and blocking it would strand every
    qualified mention from its own plain surface form. The chosen failure
    direction favours coalescence — an over-merge is visible and recoverable,
    whereas an entity that never coalesces fragments the graph silently.
    """
    ta = set(re.findall(r"\d[\w.\-]*", a.lower()))
    tb = set(re.findall(r"\d[\w.\-]*", b.lower()))
    if ta != tb:
        return False
    qa, qb = _qualifier_signature(a), _qualifier_signature(b)
    # Only a CONFLICT between two present qualifiers blocks the merge.
    return not qa or not qb or qa == qb


def _is_valid_entity(name: str, blocklist: frozenset[str] | None = None) -> bool:
    """Reject obviously generic names that are not real named entities."""
    bl = blocklist if blocklist is not None else ENTITY_NAME_BLOCKLIST
    if len(name) < MIN_ENTITY_NAME_LENGTH or name.lower() in bl:
        return False
    # CAURA graph-build fix (A): drop literal values + attribute/field names. Dropping
    # the node cascades to its edges — relations require both endpoints to be persisted
    # nodes (relation loop: `if from_id and to_id`).
    if _LITERAL_OR_ATTR_RE.match(name.strip()):
        return False
    return True


async def _discover_cross_links_for_memory(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
) -> None:
    """Run cross-link discovery for a single memory after entity extraction.

    DB-free as of Fix 2 Ph6: the discover-cross-links step folds its candidate
    read + LATERAL match + ON-CONFLICT insert into one atomic core-storage-api
    call, so this calls the storage client directly (the single-step
    PipelineContext indirection — and its ``async_session`` — is no longer
    needed). Targeted mode keys off ``target_memory_ids``.
    """
    resp = await get_storage_client().discover_cross_links(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        batch_size=CROSS_LINK_MEMORY_BATCH_SIZE,
        threshold=CROSS_LINK_SIMILARITY_THRESHOLD,
        text_verify=CROSS_LINK_TEXT_VERIFY,
        target_memory_ids=[memory_id],
    )
    links = resp.get("links_created", 0)
    if links:
        logger.info(
            "Cross-link discovery created %d links for memory %s",
            links,
            memory_id,
        )


async def _purge_written_artifacts_if_dropped(
    sc: Any, memory_id: UUID, tenant_id: str
) -> Literal["live", "dropped", "unknown"]:
    """Undo our own graph writes when the memory died while we were making them.

    Reports WHAT IT FOUND and leaves the policy to the caller, because the two
    call sites want different things from the same answer. An earlier revision
    returned a bool meaning "you must stop", which forced the indeterminate case
    to pick one of the two real answers and pretend — it chose "stop", and that
    silently discarded the audit-log entry, the contradiction trigger and
    cross-link discovery for a memory that a transient read timeout had said
    nothing bad about. Three states, named:

    ``dropped``
        The row is gone or soft-deleted, and its graph rows have been purged
        (or the purge failed, loudly — either way it is not coming back).
    ``live``
        The row is there. Nothing was purged and nothing should stop.
    ``unknown``
        The read itself failed. Nothing is known and nothing was purged.

    Purging is only half of the job at the first call site: everything after the
    link upsert — relation upserts carrying ``evidence_memory_id``, the subject
    write-back, cross-link discovery — writes MORE graph rows for this memory. A
    version that purged and fell through cleaned the table and then immediately
    refilled it, moving the leak from the link table to the relation table rather
    than closing it.

    H-02. The liveness check before the persistence block narrows the window; it
    cannot close it. Between that check and the links landing there are embedding
    round-trips and three storage calls, and a governance drop can complete
    inside them — including its own purge, which finds nothing because these rows
    do not exist yet. The entities then land moments later and nothing ever
    revisits them: the memory is gone, so no verdict names it again.

    Re-checking AFTER the writes closes it, and the argument is about what is
    observable rather than about timing:

    * the drop committed before our writes — its purge found nothing, but this
      check sees the row deleted, and we purge what we just wrote,
    * the drop commits after our writes — its own purge sees our rows and takes
      them,
    * the drop commits between the two — whichever purge runs later sees the
      rows, and both are keyed on the same ``memory_id``.

    That argument only holds for rows written BEFORE the call, which is why
    ``process_entity_extraction`` calls this THREE times rather than once. An
    earlier revision called it once, after the link upsert, and claimed "there is
    no ordering left in which the rows survive" — untrue of everything written
    afterwards: the subject write-back, the relation upserts, the links
    cross-link discovery creates. The call sites, and what each is for:

    * after the link upsert — an optimisation. It saves the relation upserts and
      a cross-link pass when the row is already gone, and is allowed to fall
      through on anything short of ``dropped``.
    * at the end of the ``try`` — the guarantee for the path that completes. It
      sits after every graph-mutating write, so the rows it can find are all of
      them.
    * in the ``except`` — the same guarantee for the path that leaves by
      raising, which the previous one cannot reach.

    Both of the trailing two are guarded on ``wrote_graph_rows``, so a run that
    wrote nothing does not pay for a read, and the ``except`` one also runs on
    failures early enough that ``sc`` does not exist yet.

    The WRITER read is load-bearing at every site for the same reason as the
    pre-write check: the whole question is whether a delete that just committed
    is visible.

    A failed PURGE still reports ``dropped``: the memory is gone whether or not
    the cleanup worked, and the caller's decision does not change. Only a failed
    READ is ``unknown``.
    """
    try:
        live = await sc.get_memory(str(memory_id), tenant_id, read=False)
    except Exception:
        logger.exception(
            "entity extraction: could not establish whether memory %s survived its own "
            "extraction; nothing purged and nothing concluded",
            memory_id,
            extra={"liveness_check": "unknown", "memory_id": str(memory_id)},
        )
        return "unknown"

    if live is not None and live.get("deleted_at") is None:
        return "live"

    try:
        counts = await sc.purge_entity_artifacts(tenant_id, str(memory_id))
    except Exception:
        logger.exception(
            "entity extraction: memory %s was dropped while its entities were being "
            "written, and the rows just written could NOT be purged; they are live in "
            "the graph for content the policy removed",
            memory_id,
        )
        return "dropped"
    if not isinstance(counts, dict):
        # Same gap as the governance-side purge: reading ``counts`` as a dict
        # would raise from outside the try above. Lower stakes at two of this
        # function's three call sites, which sit inside the worker's catch-all —
        # but NOT at the one in the ``except`` handler, where a raise escapes
        # ``process_entity_extraction`` entirely and surfaces as an unhandled
        # task exception. The purge still happened as far as anything here can
        # tell; only the counts are unreadable.
        logger.warning(
            "entity extraction: memory %s was dropped mid-extraction and the purge "
            "answered with %s rather than an object, so what it removed is unknown",
            memory_id,
            type(counts).__name__,
        )
        return "dropped"
    logger.warning(
        "entity extraction: memory %s was dropped mid-extraction; purged the graph "
        "rows just written for it (links=%s relations=%s entities=%s)",
        memory_id,
        counts.get("links"),
        counts.get("relations"),
        counts.get("entities"),
    )
    return "dropped"


async def process_entity_extraction(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    content: str,
    memory_type: str,
) -> None:
    # CAURA-595: today this runs in-process in core-api (every scheduler
    # in the codebase wraps it in `track_task`). That satisfies the
    # literal "off the hot path" framing but not the original intent of
    # the scaling plan, which was to land the work on a dedicated worker
    # fleet so core-api isn't CPU/memory-contended by burst-time LLM
    # calls. Full migration: CAURA-593 lands Pub/Sub first, then a new
    # worker service subscribes to ``Topics.Pipeline.ENTITY_EXTRACT_REQUESTED``
    # and this function becomes its handler body.
    #
    # H-02. Guards both of the trailing liveness checks — the one at the end of
    # the ``try`` and the one in the ``except``. It means "this memory MAY have
    # graph rows", not "the persistence block ran": there are two independent
    # writers below (the entity/link upserts, and cross-link discovery, which
    # runs even when every extracted name was filtered out), and both set it.
    #
    # False means nothing was written, so there is nothing to purge and no reason
    # to spend a writer read. That matters because the early exits above the
    # persistence block — no entities extracted, row already gone — are the
    # common case, and because ``sc`` is not yet bound on the first of them.
    wrote_graph_rows = False
    try:
        # A5c: resolve tenant_config BEFORE the extraction call so the
        # tenant-level ``entity_extraction.provider`` / ``.model``
        # overrides on ResolvedConfig actually take effect. Pre-A5c the
        # worker passed nothing here, falling back to global settings,
        # so per-tenant routing was dead code.
        from core_api.services.organization_settings import resolve_config

        tenant_cfg = await resolve_config(tenant_id)

        graph = await extract_entities_from_content(content, memory_type, tenant_config=tenant_cfg)
        if not graph.entities:
            return

        sc = get_storage_client()

        # H-02: is the memory still there? This is scheduled fire-and-forget at
        # write time on both non-inline paths, in parallel with the enrichment
        # that carries the governance verdict — so by the time the LLM call above
        # returns, the policy may already have dropped the row. Writing entities
        # for it would re-create the leak the drop exists to close, in a table
        # the drop does not reach.
        #
        # ``read=False`` — the WRITER. The whole point is to observe a delete
        # that just committed; a replica under lag would report the row live and
        # this check would pass exactly when it most needed to fail.
        #
        # This check alone does NOT close the window, and it is not claimed to:
        # the writes below are several round-trips away, so a drop can land after
        # it passes. ``_purge_written_artifacts_if_dropped`` at the end of the
        # persistence block is what closes that; this one is here to avoid doing
        # the work at all in the common case where the row is already gone.
        live = await sc.get_memory(str(memory_id), tenant_id, read=False)
        if live is None or live.get("deleted_at") is not None:
            logger.info(
                "entity extraction: memory %s is gone by the time extraction finished; "
                "discarding %d extracted entit(ies) rather than writing them to a "
                "dropped row's graph",
                memory_id,
                len(graph.entities),
            )
            return

        blocklist = tenant_cfg.entity_blocklist

        # ---- Filter + dedupe entities up-front ----
        #
        # The old serial path interleaved blocklist filtering with
        # per-entity HTTPs. Collapsing into the bulk path means filtering
        # first so the resolve / upsert / link batches don't carry
        # already-rejected names. Duplicate ``canonical_name`` values in
        # ``graph.entities`` (the LLM occasionally repeats them across
        # mentions) collapse to the FIRST occurrence here — preserves
        # today's role binding (``entity_roles[ent.canonical_name] =
        # ent.role`` in the old serial path also picked first-wins).
        #
        # WT-2: dedupe on ``canonical_match_key`` rather than the raw
        # string, so one extraction that returns two surface forms of the
        # SAME subject — ``analytics service`` twice, ``Analytics Service``
        # vs ``analytics service``, or ``new analytics service`` alongside
        # ``analytics service`` — mints ONE entity and ONE link instead of
        # duplicating both. First occurrence wins (surface form, type, and
        # role alike), same first-wins contract as before.
        filtered: list[tuple[str, str, str]] = []  # (canonical_name, entity_type, role)
        seen_names: set[str] = set()
        for ent in graph.entities:
            if not _is_valid_entity(ent.canonical_name, blocklist):
                logger.debug("Skipping invalid entity name '%s'", ent.canonical_name)
                continue
            match_key = canonical_match_key(ent.canonical_name)
            if match_key in seen_names:
                continue
            seen_names.add(match_key)
            filtered.append((ent.canonical_name, ent.entity_type, ent.role))

        if not filtered:
            # Nothing to persist; skip the bulk flow but keep the
            # downstream audit-log + contradiction-trigger paths so a
            # zero-entity memory still records the run.
            name_to_id: dict[str, UUID] = {}
        else:
            # ---- Step 1: parallel embeddings (audit P1) ----
            #
            # Replaces the per-entity ``await get_embedding(...)`` loop
            # with one ``asyncio.gather`` round. ``return_exceptions=True``
            # carries the prior skip-on-failure semantics — a single
            # entity that fails to embed becomes ``None`` in its slot
            # rather than aborting the whole batch.
            # ``tenant_cfg`` (resolved above for the extraction call) also
            # routes the embeds: tenant provider/key overrides apply, and
            # resolution can't fall to the raw env default — entity vectors
            # must live in the same space as this tenant's memory vectors.
            embed_results = await asyncio.gather(
                *(get_embedding(name, tenant_cfg, background=True) for name, _et, _role in filtered),
                return_exceptions=True,
            )
            name_embeddings: dict[str, list[float] | None] = {}
            for (name, _et, _role), emb in zip(filtered, embed_results):
                # ``BaseException`` — ``asyncio.gather(return_exceptions=
                # True)`` captures ALL ``BaseException`` subclasses as
                # result values, not just ``Exception``. The narrower
                # ``isinstance(emb, Exception)`` check would silently
                # store ``CancelledError`` (and other ``BaseException``
                # subclasses) as if it were a valid embedding, since
                # ``CancelledError`` inherits directly from
                # ``BaseException`` in Python 3.8+. Trade-off vs the
                # pre-P1 per-entity ``try/except Exception`` shape: we
                # now drop cancellations to ``None`` and continue
                # instead of letting them propagate; preferable to
                # corrupting the embedding payload with an exception
                # instance.
                if isinstance(emb, BaseException):
                    logger.debug(
                        "Failed to embed entity name '%s', skipping fuzzy resolution",
                        name,
                    )
                    name_embeddings[name] = None
                else:
                    name_embeddings[name] = emb

            # ---- Step 2a: bulk resolve ----
            #
            # One HTTP replaces N x (find_exact + optional similarity).
            # Storage-side ``/entities/bulk-resolve`` mirrors the
            # ``upsert_entity`` precedence: exact match first, then
            # cosine similarity (Phase 2) only when ``name_embedding``
            # is non-null and no exact match was found.
            resolve_items = [
                {
                    "input_idx": i,
                    "fleet_id": fleet_id,
                    "canonical_name": name,
                    "entity_type": entity_type,
                    "name_embedding": name_embeddings.get(name),
                }
                for i, (name, entity_type, _role) in enumerate(filtered)
            ]
            resolved = await sc.bulk_resolve_entities(
                tenant_id=tenant_id,
                items=resolve_items,
                threshold=ENTITY_RESOLUTION_THRESHOLD,
            )

            # Storage-side contract: ``bulk_resolve_entities`` returns
            # one slot per input item (``None`` for no-match,
            # match-dict otherwise). A shorter response indicates a
            # storage-layer partial failure — surface it explicitly so
            # the implicit "items beyond ``len(resolved)`` fall through
            # to the create branch" behaviour below isn't a silent
            # data-divergence path. The ``resolved[i] if i <
            # len(resolved) else None`` guard a few lines down still
            # carries the safe default.
            if len(resolved) != len(filtered):
                logger.warning(
                    "bulk_resolve_entities returned %d result(s) for %d input(s); "
                    "items beyond index %d treated as no-match (create path) — "
                    "check for storage-layer partial failures",
                    len(resolved),
                    len(filtered),
                    # ``max(0, ...)`` avoids a confusing "beyond index -1"
                    # when storage returns an empty list (all items
                    # treated as no-match starting from index 0).
                    max(0, len(resolved) - 1),
                )

            # ---- Step 2b: client-side merge — first-seen-wins canonical ----
            #
            # CRITICAL correctness gate. Mirrors ``entity_service.upsert_entity``
            # lines 74-100 exactly: when an existing entity is found
            # (exact or similarity), the EXISTING ``canonical_name`` wins
            # ("first-seen-wins"), and the new surface form is added to
            # ``_aliases``. Comment at entity_service.py:91-100 warns
            # about the prior "longest-wins" regression that turned LLM
            # hallucinations into canonical rows — this preservation is
            # the audit P1 fix's correctness gate.
            upsert_items: list[dict] = []
            for i, (name, entity_type, _role) in enumerate(filtered):
                match = resolved[i] if i < len(resolved) else None
                # CAURA graph-build fix (B): reject a similarity-merge when the two
                # names carry DIFFERENT identifier tokens (e.g. "#0002" vs "#0012").
                # Forces the create path so suffix-distinct entities stay separate.
                if match and not _same_identifier_signature(name, match.get("canonical_name") or ""):
                    match = None
                item: dict = {
                    "input_idx": i,
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": entity_type,
                }
                emb = name_embeddings.get(name)
                if emb is not None:
                    item["name_embedding"] = emb

                if match:
                    # Existing row found — merge into it.
                    existing_attrs = match.get("attributes") or {}
                    merged_attrs = dict(existing_attrs)
                    # NOTE: ``ExtractedEntity`` currently emits no extra
                    # attributes (only ``canonical_name`` / ``entity_type``
                    # / ``role`` come back from the LLM). If the
                    # extraction schema later grows attribute fields,
                    # add ``merged_attrs.update(<new fields>)`` here to
                    # match ``entity_service.upsert_entity`` (line 79:
                    # ``if data.attributes: merged_attrs.update(data.attributes)``)
                    # and keep the bulk path's merge semantics
                    # equivalent to the single-row serial path.
                    aliases = list(merged_attrs.get("_aliases") or [])
                    # Defensive fallback: storage-side ``bulk_resolve_entities``
                    # SHOULD always carry a non-empty ``canonical_name`` on a
                    # match (the row had to exist for a match to fire). An
                    # empty / missing value here would degenerately become
                    # the new canonical under first-seen-wins, which is
                    # worse than just using the incoming name. Log + fall
                    # back so a malformed resolve response gets surfaced
                    # rather than silently corrupting the entity row.
                    existing_name = match.get("canonical_name") or ""
                    if not existing_name:
                        logger.warning(
                            "bulk_resolve_entities match for '%s' has no canonical_name; "
                            "falling back to incoming name",
                            name,
                        )
                        existing_name = name
                    if existing_name and existing_name not in aliases:
                        aliases.append(existing_name)
                    if name not in aliases:
                        aliases.append(name)
                    merged_attrs["_aliases"] = aliases
                    # Defensive guard mirroring the ``canonical_name``
                    # fallback above: a malformed resolve match without
                    # ``entity_id`` would otherwise crash with KeyError
                    # inside the upsert payload. Fall through to the
                    # create path so the row still lands; log so the
                    # storage-side data hygiene issue is surfaced.
                    match_entity_id = match.get("entity_id")
                    if not match_entity_id:
                        logger.warning(
                            "bulk_resolve_entities match for '%s' has no entity_id; "
                            "falling back to create path",
                            name,
                        )
                        item["action"] = "create"
                        item["canonical_name"] = name
                        item["attributes"] = {}
                    else:
                        item["action"] = "update"
                        item["entity_id"] = match_entity_id
                        item["canonical_name"] = existing_name  # first-seen wins
                        item["attributes"] = merged_attrs
                else:
                    # No match — create.
                    item["action"] = "create"
                    item["canonical_name"] = name
                    item["attributes"] = {}
                upsert_items.append(item)

            # ---- Step 2c: bulk upsert ----
            #
            # Returns ``{input_idx, entity_id, action}`` per row, where
            # ``action`` may be ``"created" | "updated" | "merged" |
            # "missing"`` (see /entities/bulk-upsert). ``merged`` covers
            # the TOCTOU race where another writer created the natural-
            # key match between our resolve and our upsert — semantically
            # equivalent to ``updated`` for the worker.
            # H-02. From here this memory MAY have graph rows, so every exit
            # from this function owes it a liveness check — including the ones
            # that leave by raising.
            #
            # Set BEFORE the await, not after. "The call raised" does not mean
            # "nothing was written": a timeout can land on a request the storage
            # side already committed, and a malformed response does not un-write
            # rows either. The flag has to mean "might have written", because
            # the only failure it is allowed to make is the cheap one — a wasted
            # writer read on a call that never landed, on a path that is already
            # an error. Getting it wrong the other way leaks the rows.
            wrote_graph_rows = True
            upserted = await sc.bulk_upsert_entities(items=upsert_items)
            # Explicit loop (not a comprehension) so an out-of-range
            # ``input_idx`` from a misbehaving storage response surfaces
            # as a WARN log instead of an IndexError → 500. Mirrors the
            # length-mismatch warning above on ``bulk_resolve_entities``
            # — same "treat malformed responses defensively" pattern.
            name_to_id: dict[str, UUID] = {}
            # H-02. Only the rows THIS run brought into existence. An entity that
            # already existed is reachable through whatever linked it before and
            # is nobody's orphan; a created one is reachable only through the link
            # we are about to write. See the link upsert below for why that
            # distinction is worth carrying.
            created_entity_ids: list[str] = []
            for r in upserted:
                if not r.get("entity_id"):
                    continue
                idx = r["input_idx"]
                if idx >= len(filtered):
                    logger.warning(
                        "bulk_upsert_entities returned out-of-range input_idx %d (filtered len=%d); skipping",
                        idx,
                        len(filtered),
                    )
                    continue
                name_to_id[filtered[idx][0]] = UUID(r["entity_id"])
                if r.get("action") == "created":
                    created_entity_ids.append(str(r["entity_id"]))

            # ---- Step 3: bulk entity-link upsert ----
            #
            # Idempotent (memory_id, entity_id) writes — pre-existing
            # rows have their role preserved, matching today's
            # ``find_entity_link → skip-if-exists`` flow.
            # ``input_idx`` must be contiguous in ``[0, len(link_items))``
            # for the storage-side ``_validate_input_idxs`` check —
            # otherwise gaps in the source ``filtered`` list (entries
            # filtered out because their upsert came back as ``missing``
            # / no entity_id) would produce non-contiguous indexes and
            # trip the 422. Use a dedicated ``link_idx`` counter so the
            # response idxs always tile the payload contiguously.
            # WT-2 Fix A: dedupe at the write site — two DIFFERENT surface
            # forms in one batch can resolve to the SAME entity row (via the
            # normalised match or embedding similarity), and the same
            # (memory, entity) pair must never be sent twice. The DB's
            # composite PK ``(memory_id, entity_id)`` is the backstop
            # (migration 001 — no new migration needed); this keeps the
            # batch itself clean and the role binding first-wins.
            link_items = []
            link_idx = 0
            linked_entity_ids: set[UUID] = set()
            for name, _et, role in filtered:
                if name not in name_to_id:
                    continue
                entity_id = name_to_id[name]
                if entity_id in linked_entity_ids:
                    continue
                linked_entity_ids.add(entity_id)
                link_items.append(
                    {
                        "input_idx": link_idx,
                        "memory_id": str(memory_id),
                        "entity_id": str(entity_id),
                        "role": role,
                    }
                )
                link_idx += 1
            if link_items:
                try:
                    link_result = await sc.bulk_upsert_entity_links(tenant_id, items=link_items)
                except Exception:
                    # H-02, and this is the one gap the purge cannot close from
                    # its own side. ``memory_purge_entity_artifacts`` finds
                    # entities THROUGH the memory's links — deliberately, because
                    # the first draft swept every unlinked entity in the tenant
                    # and would have raced a concurrent writer that had created
                    # an entity but not yet linked it. Over-deleting is the
                    # direction that does not come back.
                    #
                    # The cost of that bounding is exactly here: entities
                    # committed a moment ago whose links never landed are
                    # reachable by nothing, so a purge for this memory runs,
                    # finds no links, and honestly reports zero. Indistinguishable
                    # in the audit trail from "there was nothing to purge" —
                    # which is what makes it worth a log rather than nothing.
                    #
                    # Widening the candidate set instead was considered and not
                    # done: passing these ids to the purge re-introduces the same
                    # race the bounding exists to prevent, since an id we upserted
                    # may be a row another writer created and is about to link.
                    # A person with a concrete list can check that; a query
                    # cannot.
                    #
                    # Re-raised, so the except handler below still runs its
                    # liveness check and purges whatever IS reachable.
                    if created_entity_ids:
                        logger.exception(
                            "entity extraction: entity rows for memory %s were committed but "
                            "their links were not, so a governance purge for this memory will "
                            "find and report nothing while these rows survive; entity ids "
                            "created by this run: %s",
                            memory_id,
                            ", ".join(created_entity_ids),
                            extra={
                                "unlinked_entity_ids": created_entity_ids,
                                "memory_id": str(memory_id),
                                "tenant_id": tenant_id,
                            },
                        )
                    raise
                # H-02: the rows exist NOW, so from here the leak is recoverable
                # by the same purge governance uses. See the helper for why this
                # is where the window actually closes.
                #
                # The RETURN has to be honoured, not just the purge. Everything
                # below writes more graph rows for this memory — relations
                # carrying ``evidence_memory_id``, the subject write-back,
                # cross-link discovery. Purging and then falling through refills
                # the table we just cleaned.
                #
                # ``dropped`` ONLY. This site is an optimisation — it saves the
                # relation upserts and a cross-link discovery pass — so it must
                # not act on ``unknown``. Stopping here also skips the audit-log
                # entry below, and losing an audit record to a read timeout is a
                # real cost with no later repair.
                #
                # Falling through on ``unknown`` is safe because a later check
                # covers every remaining exit: the one at the end of the ``try``
                # if the rest of the run succeeds, the one in the ``except`` if
                # it raises. Neither is a guarantee that the rows are cleaned —
                # both call the same read, and a read that failed once will
                # likely fail again — but the failure is a bounded leak that
                # governance's purge can still reach, and the alternative was
                # destroying an audit record nothing rebuilds.
                if await _purge_written_artifacts_if_dropped(sc, memory_id, tenant_id) == "dropped":
                    return
                # Surface any FK violations from the per-item path
                # (storage-side reports ``error="fk_violation"`` for rows
                # whose memory_id or entity_id no longer exists). Same
                # observability shape we added on the contradiction
                # detector's batch path.
                fk_errors = [r for r in link_result if r.get("error")]
                if fk_errors:
                    logger.warning(
                        "Entity link upsert: %d/%d row(s) failed with FK violation for memory %s",
                        len(fk_errors),
                        len(link_items),
                        memory_id,
                    )

            # ---- Step 3b: subject write-back (A63) ----
            #
            # ``memories.subject_entity_id`` is NULL on nearly every row
            # because the write-time triple path (CAURA-123) only fires on
            # narrow predicate regexes — which keeps the deterministic RDF
            # contradiction path and the A1 #17 cross-subject preflight
            # dormant, and blocks subject-scoped candidate selection.
            # The extraction LLM already names a subject: when the links
            # we just wrote contain EXACTLY ONE distinct subject-role
            # entity, write it back. Ambiguity (0 or 2+ subjects) skips —
            # a wrong subject is worse than none, because downstream
            # gates treat the column as authoritative (A1 #17 compares it
            # across rows). Storage-side the update is guarded by
            # ``subject_entity_id IS NULL`` so a triple-path value or a
            # concurrent delivery's write is never clobbered.
            subject_ids = {
                str(name_to_id[name])
                for name, _et, role in filtered
                if role == "subject" and name in name_to_id
            }
            if len(subject_ids) == 1:
                subject_id = next(iter(subject_ids))
                try:
                    updated = await sc.set_subject_entity_if_null(
                        memory_id=str(memory_id),
                        tenant_id=tenant_id,
                        subject_entity_id=subject_id,
                    )
                    logger.info(
                        "subject_writeback memory=%s subject_entity_id=%s outcome=%s",
                        memory_id,
                        subject_id,
                        "set" if updated else "kept_existing",
                    )
                except Exception:
                    # Non-fatal: the row simply stays subject-less, which
                    # is exactly today's behaviour.
                    logger.warning(
                        "subject_writeback failed for memory %s (non-fatal)",
                        memory_id,
                        exc_info=True,
                    )
            elif len(subject_ids) > 1:
                logger.info(
                    "subject_writeback memory=%s outcome=skipped_ambiguous n_subjects=%d",
                    memory_id,
                    len(subject_ids),
                )
            else:
                # The silent branch. "Set a subject" and "found two and refused"
                # both logged; "no entity claimed one" logged nothing, so a row
                # that will never take part in subject-scoped contradiction
                # detection looked identical to a row that was never processed
                # — the same "ran and found nothing" vs "never ran" ambiguity
                # D3 closed for the contradiction detector.
                #
                # Zero subjects is the SIGNATURE of a degraded extraction:
                # ``_fake_extract`` stamps role="mentioned" on everything it
                # finds, so a heuristic graph never names a subject. Together
                # with ``entity_extraction_degraded_to_heuristic`` this makes
                # the degrade countable from the outside, per memory.
                logger.info(
                    "subject_writeback memory=%s outcome=skipped_no_subject n_links=%d",
                    memory_id,
                    len(filtered),
                )

        # Upsert relations. Endpoints resolve by raw name first (today's
        # contract), then by ``canonical_match_key`` — the WT-2 dedupe above
        # can collapse a surface form out of ``filtered`` (e.g. ``analytics
        # service`` deduped under ``new analytics service``), and a relation
        # that names the collapsed form must still land on the merged row.
        key_to_id: dict[str, UUID] = {canonical_match_key(n): i for n, i in name_to_id.items()}
        rel_count = 0
        for rel in graph.relations:
            from_id = name_to_id.get(rel.from_entity) or key_to_id.get(canonical_match_key(rel.from_entity))
            to_id = name_to_id.get(rel.to_entity) or key_to_id.get(canonical_match_key(rel.to_entity))
            if from_id and to_id:
                await upsert_relation(
                    RelationUpsert(
                        tenant_id=tenant_id,
                        fleet_id=fleet_id,
                        from_entity_id=from_id,
                        relation_type=rel.relation_type,
                        to_entity_id=to_id,
                        evidence_memory_id=memory_id,
                    ),
                )
                rel_count += 1

        # Audit log
        await log_action(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="entity_extraction",
            resource_type="memory",
            resource_id=memory_id,
            detail={
                "entities_count": len(name_to_id),
                "relations_count": rel_count,
            },
        )

        logger.info(
            "Entity extraction complete for memory %s: %d entities, %d relations",
            memory_id,
            len(name_to_id),
            rel_count,
        )

        # Trigger entity-based contradiction detection now that entity links exist
        if name_to_id:
            from core_api.services.contradiction import (
                Trigger,
                run_contradiction_detection,
            )
            from core_api.tasks import track_task

            track_task(run_contradiction_detection(memory_id, tenant_id, fleet_id, trigger=Trigger.ENTITY))

        # Cross-link discovery (non-fatal)
        if tenant_cfg.auto_entity_linking_enabled:
            # H-02. This writes ``memory_entity_links`` rows for THIS memory —
            # storage-side it is an ON CONFLICT DO NOTHING insert into exactly
            # the table the purge deletes from — and it does not go through the
            # persistence block above, so it is a second, independent way for
            # this memory to acquire graph rows. It runs even when every
            # extracted name was filtered out and ``bulk_upsert_entities`` was
            # never called.
            #
            # Before the await, for the same reason as the upsert: a call that
            # raised may still have committed.
            wrote_graph_rows = True
            try:
                await _discover_cross_links_for_memory(memory_id, tenant_id, fleet_id)
            except Exception:
                logger.warning(
                    "Cross-link discovery failed for memory %s (non-fatal)",
                    memory_id,
                    exc_info=True,
                )

        # H-02, and this is the call that actually closes the window. The check
        # after the link upsert only covers rows written UP TO it; everything
        # between the two — the subject write-back, the relation upserts carrying
        # ``evidence_memory_id``, the links cross-link discovery creates — is
        # written after it has already passed. A drop landing in that stretch
        # runs its own purge against rows that do not exist yet, and nothing
        # revisits them.
        #
        # This one runs after every graph-mutating write for this memory, so the
        # rows it finds are all of them. The earlier check is kept as an early
        # exit: it saves the relation upserts and a cross-link discovery pass in
        # the common case where the row was already gone.
        #
        # It covers the SUCCESSFUL path only. The ``except`` handler carries the
        # same check for the path where something in between raised.
        #
        # Contradiction detection above is deliberately not covered here. It is
        # spawned via ``track_task`` and writes conflict rows rather than graph
        # rows, and it re-checks ``deleted_at`` itself for exactly this race.
        #
        # Guarded on the flag, so a run that reached here having written nothing
        # — every extracted name filtered out AND cross-link discovery disabled —
        # does not pay for a writer read to find nothing. Note what the flag has
        # to mean for that to be safe: "this memory may have graph rows", not
        # "the persistence block ran". Cross-link discovery sets it too, and
        # gating on the narrower reading would leak exactly the links it creates.
        #
        # The result is not branched on: nothing follows this, so ``live`` and
        # ``unknown`` are the same instruction — do nothing — and ``dropped`` has
        # already purged by the time it returns.
        if wrote_graph_rows:
            await _purge_written_artifacts_if_dropped(sc, memory_id, tenant_id)

    except Exception:
        logger.exception("Entity extraction failed for memory %s (non-fatal)", memory_id)
        # H-02, and the reason the check below is duplicated rather than moved
        # into a ``finally``. The normal-path call at the end of the ``try`` is
        # unreachable once anything between the link upsert and it raises — the
        # relation upserts, the subject write-back, the audit log — and the rows
        # already written stay behind for a memory that may have been dropped.
        # That is the leak this PR exists to close, arriving by a different door.
        #
        # A ``finally`` would cover this in one place, but it also fires on the
        # three early ``return``s in the ``try``, and all three are wrong for it:
        # the no-entities exit happens before ``sc`` is even bound, so an
        # unguarded ``finally`` raises ``NameError`` out of a fire-and-forget
        # task; the already-dropped exit would spend a writer read to learn what
        # it just learned; and the ``dropped`` exit would repeat a purge that had
        # just run. Those are the common paths, not the rare ones. The flag would
        # have to gate a ``finally`` anyway, so all ``finally`` buys is one fewer
        # call site.
        #
        # Nothing is branched on. ``dropped`` has purged by the time it returns;
        # ``live`` and ``unknown`` both mean leave it alone. ``unknown`` is the
        # likely answer when the storage failure that landed us here is still
        # going, and the rows do leak in that case — bounded to what was written
        # before the raise, and reachable by governance's own purge later. The
        # alternative was losing the audit record outright.
        if wrote_graph_rows:
            await _purge_written_artifacts_if_dropped(sc, memory_id, tenant_id)
