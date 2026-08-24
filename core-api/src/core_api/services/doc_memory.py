"""Mint a memory carrying a document's body on every document write.

Why this exists
---------------
The two stores are not cross-searched: ``caura_recall`` never returns
documents, and a document row embeds only ``data["summary"]`` — never its body.
So a document's *content* is unreachable by meaning; you can only find it if you
already know its ``collection`` + ``doc_id``. Minting a memory that carries the
body verbatim closes that gap for the paths that read ``memories.content``
directly (``caura_recall`` and the recall brief, neither of which truncates).

Rewrite semantics come free from ``write_mode="fast"``
-----------------------------------------------------
Every doc write runs this same path — there is no create-vs-update branch and no
lookup of previously minted rows. That is not as duplicative as it sounds,
because the fast write pipeline (``pipeline/compositions/write.py``) already
runs ``CheckExactDuplicate`` then ``DetectNearDuplicate``:

  * identical body      -> ``CheckExactDuplicate`` raises 409, no row is written.
                           Idempotent doc re-syncs self-dedupe.
  * changed body, close -> row is written PLUS ``metadata["near_duplicate_of"]``
                           pointing at the previous version (advisory only at
                           ``SEMANTIC_DEDUP_JUDGE_THRESHOLD``; it does not reject).
  * changed body, far   -> row is written, no link.

Known limitation: nothing outdates or supersedes the older rows, so a doc edited
N times leaves N active memories and recall can surface several versions.
``metadata["near_duplicate_of"]`` is the thread a future reconciliation pass
would follow.

Contract for callers
--------------------
Use ``safe_sync_doc_memory``. It never raises: the document is the source of
truth and must never fail to persist because a derived memory could not be
written.

Latency: this runs INLINE, deliberately
---------------------------------------
``write_mode="fast"`` defers only ENRICHMENT (the LLM call). Everything else in
the write pipeline still runs awaited inside the caller's request:
``ParallelEmbedEnrich`` (embedding), ``CheckExactDuplicate``,
``DetectNearDuplicate``, ``WriteMemoryRow``. Measured on a VM against real
providers, that is ~210 ms per document write:

    write_mode=fast  embedding_ms=151  dedup_lookup_ms=9  storage_ms=16
    total_ms=210     enrichment_pending=True

plus a ``resolve_config`` read and a ``get_or_create_agent`` upsert. So a doc
write that previously only embedded a short summary now costs ~210 ms more.

Accepted for now. The handlers are ``async def`` and every wait is on I/O, so
the event loop keeps serving other requests throughout — the cost is added to
the calling request's response, not to server throughput. For interactive
one-document-at-a-time writes that is not worth optimising.

Deliberately NOT moved to ``track_task``, which is ``asyncio.create_task`` —
same process, no queue and no durability. Backgrounding would hide the 210 ms
but would also mean a doc write can return 200 while its memory never
materialises: nothing retries, and ``cancel_all_tasks()`` kills in-flight tasks
on shutdown, so a write landing during a deploy would silently lose its memory.
Today a 200 guarantees the memory exists, which is the stronger property.

Revisit if bulk doc sync becomes a real workload — at ~210 ms each, 10k
documents is ~35 minutes of added wall-clock. The cheapest win then is dropping
the unconditional ``get_or_create_agent`` round-trip (on the MCP path
``enforce_fleet_write`` has already registered the caller), before reaching for
a durable queue.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from core_api.agent_ids import DOC_INDEXER_AGENT_ID
from core_api.constants import CHUNKING_THRESHOLD_CHARS
from core_api.services.doc_indexing import DocMemorySpec

logger = logging.getLogger(__name__)


async def resolve_doc_memory_agent(
    tenant_id: str,
    caller_agent_id: str | None,
    fleet_id: str | None,
) -> str:
    """Identity to attribute the minted memory to, registered if new.

    Prefers the real document writer. Attribution is not cosmetic:
    ``caura_insights`` defaults to ``scope="agent"``, which filters
    ``Memory.agent_id == agent_id``, so a row attributed to the service identity
    is invisible to every real agent's default insights run.

    Falls back to ``DOC_INDEXER_AGENT_ID`` only when the caller has no identity
    at all (REST ``POST /documents`` with no gateway-stamped ``X-Agent-ID``).

    Registration is unconditional, and that matters. The MCP write path calls
    ``enforce_fleet_write`` before minting, which registers the caller on first
    contact — but REST ``POST /documents`` has no equivalent step. Returning the
    caller's id without registering it produced a memory attributed to an
    identity with no ``agents`` row, which ``caura_insights`` then refuses to
    run for ("Agent X is not registered"), making the minted memory unreachable
    by the default ``scope="agent"`` pass. Found in the wet test on
    ``eyal-wet-tests``. ``get_or_create_agent`` is an idempotent upsert, so the
    MCP path just re-asserts what ``enforce_fleet_write`` already did.
    """
    from core_api.services.agent_service import get_or_create_agent

    agent_id = caller_agent_id or DOC_INDEXER_AGENT_ID
    await get_or_create_agent(
        tenant_id,
        agent_id,
        fleet_id,
        display_name=None if caller_agent_id else "Caura Doc Indexer",
    )
    return agent_id


async def sync_doc_memory(
    spec: DocMemorySpec,
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str | None,
) -> str | None:
    """Write the doc-derived memory. Returns its id, or ``None`` if skipped.

    Raises on failure — callers should use ``safe_sync_doc_memory``.
    """
    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import create_memory
    from core_api.services.organization_settings import resolve_config

    # Auto-chunk guard. When the tenant has enabled chunking and the body is
    # over the threshold, ``create_memory`` writes a parent PLUS N child
    # memories that all inherit this same ``source_uri``. Nothing is lost (the
    # parent keeps the full body), but one doc then maps to N+1 rows, which
    # defeats the provenance key and multiplies the recall footprint. Skip
    # instead. Inert for nearly every tenant: ``auto_chunk_enabled`` defaults
    # to False.
    #
    # KNOWN TRADEOFF, chosen deliberately for simplicity: skipping means a
    # tenant with ``auto_chunk_enabled=True`` gets NO recall-reachable memory
    # for any body over ``CHUNKING_THRESHOLD_CHARS`` — and those are exactly the
    # tenants who opted in because they write long content, so the feature is
    # weakest where it would matter most. Letting chunking run would in fact
    # work (the parent row carries the verbatim body and this ``source_uri``),
    # so this guard buys one-row-per-doc rather than correctness. Revisit
    # together with the doc-version reconciliation pass, which has to handle
    # multiple rows per ``source_uri`` anyway.
    tenant_config = await resolve_config(tenant_id)
    if tenant_config.auto_chunk_enabled and len(spec.content) > CHUNKING_THRESHOLD_CHARS:
        logger.info(
            "doc_memory: skipped mint for %s (auto-chunking enabled and body is %d chars)",
            spec.source_uri,
            len(spec.content),
        )
        return None

    resolved_agent_id = await resolve_doc_memory_agent(tenant_id, agent_id, fleet_id)

    created = await create_memory(
        MemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=resolved_agent_id,
            # ``memory_type`` is deliberately NOT passed — the enrichment
            # classifier owns it. A document is not one kind of thing: a
            # decision record classifies as ``decision``, a runbook as ``rule``,
            # an incident writeup as ``episode``. Forcing every doc to ``fact``
            # would discard that. Because we don't pass it, it never enters
            # ``model_fields_set`` and so never reaches
            # ``agent_provided_fields`` — the classifier's value stands.
            #
            # Consequence to be aware of: the row is INSERTed with the schema
            # default (``fact``) and the real type lands when enrichment
            # completes a few seconds later, so a reader in that window sees
            # ``fact``. Per-type ``TYPE_DECAY_DAYS`` also means doc memories
            # will decay at different rates depending on how they classified.
            content=spec.content,
            source_uri=spec.source_uri,
            metadata=spec.metadata,
            visibility="scope_team",
            # ``status``, by contrast, IS pinned — it is lifecycle state, not a
            # semantic category, and "active" is simply correct: this row
            # mirrors the CURRENT content of a live document.
            #
            # The enrichment prompt offers "active" | "pending" | "confirmed"
            # and picks per content (a runbook reads as "confirmed"), while
            # ``insights_query_patterns`` — and stale/failures — select
            # ``status == "active"`` only. Left to the classifier, whether a
            # document ever reaches insights would hinge on a non-deterministic
            # per-document choice: prose lands "active" and is visible, an
            # operational rule lands "confirmed" and is silently invisible.
            # Observed in the wet test on ``eyal-wet-tests``: a runbook body
            # produced status="confirmed" and insights returned
            # memories_analyzed=0.
            #
            # The pin holds because of CAURA-716: the inline enrichment path
            # used to drop ``agent_provided_fields`` and infer caller intent
            # from value-vs-default, which cannot see a field pinned TO its own
            # default — so this exact ``status="active"`` was silently
            # overwritten. Don't reintroduce that heuristic.
            status="active",
            # ``fast`` deliberately, and it is load-bearing: it skips the strong
            # path's inline ``CheckSemanticDuplicate`` (which 409-rejects at
            # 0.95) while keeping ``CheckExactDuplicate`` and the advisory
            # ``DetectNearDuplicate``. That combination is exactly the rewrite
            # behaviour described in the module docstring.
            write_mode="fast",
        )
    )
    logger.info("doc_memory: minted %s for %s", created.id, spec.source_uri)
    return str(created.id)


async def safe_sync_doc_memory(
    spec: DocMemorySpec,
    *,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str | None,
) -> str | None:
    """``sync_doc_memory`` that never raises.

    The document write has already committed by the time this runs. The document
    is the source of truth, so a failure to derive a memory from it must not turn
    a successful write into an error for the caller.
    """
    try:
        return await sync_doc_memory(
            spec,
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            # EXPECTED on an idempotent doc re-write: the body is byte-identical
            # to an existing memory, so ``CheckExactDuplicate`` rejected it and
            # the memory we would have created already exists. Not a failure —
            # log at info so a no-op re-sync does not emit a stack trace.
            logger.info(
                "doc_memory: memory already exists for %s (identical content)",
                spec.source_uri,
            )
        else:
            logger.warning(
                "doc_memory: mint failed for %s (%s): %s",
                spec.source_uri,
                exc.status_code,
                exc.detail,
            )
        return None
    except Exception:
        logger.exception("doc_memory: mint failed for %s", spec.source_uri)
        return None
