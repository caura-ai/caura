"""Background worker: extract entities from a memory and upsert them."""

import asyncio
import logging
import re
from uuid import UUID

from common.embedding import get_embedding
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


def _same_identifier_signature(a: str, b: str) -> bool:
    """CAURA graph-build fix (B): two names may only merge if they carry the SAME set
    of digit-bearing identifier tokens. Synthetic suffix-distinct names like
    'comet #0002' vs 'comet #0012' embed near-identically and trip the 0.85 similarity
    merge, collapsing distinct entities into one contaminated mega-node."""
    ta = set(re.findall(r"\d[\w.\-]*", a.lower()))
    tb = set(re.findall(r"\d[\w.\-]*", b.lower()))
    return ta == tb


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
    try:
        # A5c: resolve tenant_config BEFORE the extraction call so the
        # tenant-level ``entity_extraction.provider`` / ``.model``
        # overrides on ResolvedConfig actually take effect. Pre-A5c the
        # worker passed nothing here, falling back to global settings,
        # so per-tenant routing was dead code.
        from core_api.services.organization_settings import resolve_config

        tenant_cfg = await resolve_config(None, tenant_id)

        graph = await extract_entities_from_content(content, memory_type, tenant_config=tenant_cfg)
        if not graph.entities:
            return

        sc = get_storage_client()

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
        filtered: list[tuple[str, str, str]] = []  # (canonical_name, entity_type, role)
        seen_names: set[str] = set()
        for ent in graph.entities:
            if not _is_valid_entity(ent.canonical_name, blocklist):
                logger.debug("Skipping invalid entity name '%s'", ent.canonical_name)
                continue
            if ent.canonical_name in seen_names:
                continue
            seen_names.add(ent.canonical_name)
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
            embed_results = await asyncio.gather(
                *(get_embedding(name) for name, _et, _role in filtered),
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
            upserted = await sc.bulk_upsert_entities(items=upsert_items)
            # Explicit loop (not a comprehension) so an out-of-range
            # ``input_idx`` from a misbehaving storage response surfaces
            # as a WARN log instead of an IndexError → 500. Mirrors the
            # length-mismatch warning above on ``bulk_resolve_entities``
            # — same "treat malformed responses defensively" pattern.
            name_to_id: dict[str, UUID] = {}
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
            link_items = []
            link_idx = 0
            for name, _et, role in filtered:
                if name not in name_to_id:
                    continue
                link_items.append(
                    {
                        "input_idx": link_idx,
                        "memory_id": str(memory_id),
                        "entity_id": str(name_to_id[name]),
                        "role": role,
                    }
                )
                link_idx += 1
            if link_items:
                link_result = await sc.bulk_upsert_entity_links(items=link_items)
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

        # Upsert relations
        rel_count = 0
        for rel in graph.relations:
            from_id = name_to_id.get(rel.from_entity)
            to_id = name_to_id.get(rel.to_entity)
            if from_id and to_id:
                await upsert_relation(
                    None,
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
            None,
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
            from core_api.services.contradiction_detector import (
                detect_contradictions_by_entities_async,
            )
            from core_api.tasks import track_task

            track_task(detect_contradictions_by_entities_async(memory_id, tenant_id, fleet_id))

        # Cross-link discovery (non-fatal)
        if tenant_cfg.auto_entity_linking_enabled:
            try:
                await _discover_cross_links_for_memory(memory_id, tenant_id, fleet_id)
            except Exception:
                logger.warning(
                    "Cross-link discovery failed for memory %s (non-fatal)",
                    memory_id,
                    exc_info=True,
                )

    except Exception:
        logger.exception("Entity extraction failed for memory %s (non-fatal)", memory_id)
