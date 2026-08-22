"""Memory Crystallizer engine — hygiene checks, health metrics, usage analysis, and memory crystallization."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    DEFAULT_MEMORY_TYPE,
    SERVER_RESERVED_MEMORY_TYPES,
)
from common.events.lifecycle_publishers import publish_crystallize_on_demand_request
from core_api.clients.storage_client import get_storage_client

try:
    from google.api_core.exceptions import GoogleAPIError
except ImportError:

    class GoogleAPIError(Exception):
        pass  # type: ignore[misc]


from core_api.constants import (
    CRYSTALLIZER_DEDUP_BATCH_SIZE,
    CRYSTALLIZER_DEDUP_NEIGHBORS,
    CRYSTALLIZER_DEDUP_THRESHOLD,
    CRYSTALLIZER_HIGH_PENDING_PCT,
    CRYSTALLIZER_HIGH_PII_COUNT,
    CRYSTALLIZER_LOW_EMBEDDING_COVERAGE_PCT,
    CRYSTALLIZER_MAX_BATCH_SIZE,
    CRYSTALLIZER_MAX_DEDUP_PAIRS,
    CRYSTALLIZER_MIN_CLUSTER_SIZE,
    CRYSTALLIZER_SHORT_CONTENT_CHARS,
    CRYSTALLIZER_STALE_DAYS,
    CRYSTALLIZER_STALE_MAX_WEIGHT,
    MEMORY_TYPES_WRITE,
)
from core_api.providers._retry import call_with_fallback, deliberate_fake_provider

logger = logging.getLogger(__name__)

MAX_AFFECTED_IDS = 20

# A report at one of these has finished; re-running it would overwrite a result.
# ``running`` is deliberately absent — see ``execute_reserved_report``.
_TERMINAL_REPORT_STATUSES = frozenset({"completed", "failed"})


# ---------------------------------------------------------------------------
# Crystallization LLM prompt
# ---------------------------------------------------------------------------

# The offered memory-type vocabulary is derived from ``MEMORY_TYPES_WRITE``
# — the same source of truth that drives the enrichment prompt in
# ``common/enrichment/_prompts.py`` and the OpenAPI schema description. Building
# the list at import time (rather than as a literal string) means future
# taxonomy shifts (CAURA-717, CAURA-XXX, …) propagate here automatically
# instead of relying on a reviewer to spot a hand-edited slug list.
_CRYSTALLIZER_TYPES_INLINE = ", ".join(f'"{t}"' for t in MEMORY_TYPES_WRITE)

CRYSTALLIZATION_PROMPT = (
    """\
You are a memory crystallizer for a business agent memory system.

You are given a batch of raw memories that may be noisy, redundant, or overlapping.
Your job is to extract clean, atomic facts from these memories.

Rules:
- Each output fact must be a single, self-contained statement
- Remove noise, filler, and conversational fragments
- Merge duplicate or overlapping information into one clean fact
- Preserve important details: names, numbers, dates, decisions
- Discard trivial or meaningless content
- Each fact should be 1-2 sentences maximum
- Assign a memory_type to each fact: one of """
    + _CRYSTALLIZER_TYPES_INLINE
    + """
- Assign a weight (0.0-1.0) based on importance

Input memories:
{memories}

Return ONLY a JSON array of objects (no markdown fences):
[{{"content": "...", "memory_type": "...", "weight": 0.0}}, ...]

If the input contains no meaningful information worth preserving, return an empty array: []
"""
)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def _reserve_report(sc, tenant_id: str, fleet_id: str | None, trigger: str):
    """The id of an in-flight report, or a freshly created one.

    Split out so the reservation can be AWAITED by a caller while the analysis
    itself runs elsewhere — see ``start_crystallization``. Returning the id is the
    only part a caller needs synchronously.

    Returns ``(report_id, is_new)``. ``is_new=False`` means a run is already in
    flight and this call must not start a second one.
    """
    running = await sc.find_running_report(tenant_id, fleet_id, report_type="crystallization")
    if running:
        return running.get("id"), False
    report = await sc.create_report(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "trigger": trigger,
            "status": "running",
            "report_type": "crystallization",
        }
    )
    return report.get("id"), True


async def start_crystallization(
    tenant_id: str,
    fleet_id: str | None = None,
    trigger: str = "manual",
    auto_crystallize: bool = True,
) -> UUID:
    """Reserve a report and run the analysis OFF the request. Returns the id.

    ``POST /crystallize`` used to AWAIT the whole run and then answer
    ``status="running"`` — a claim that was never true, since the run had already
    finished by the time the response was written.

    That only appeared to work because the run aborted almost immediately: an
    uncaught 409 on the first crystallized fact ended it in about a second (and
    wedged the report, which is the rest of this change). With the 409 handled the
    run does its actual work — hygiene checks across the tenant's memories, then a
    create per extracted fact, each with its own embedding and dedup lookups — and
    on a non-trivial tenant that does not fit in a request. Measured on a ~19k-row
    database: about 1s aborting, past the request timeout when completing.

    So the run moves off the request and the response becomes true. The report id
    is still returned synchronously, and ``GET /crystallize/reports`` is how a
    caller follows it — which is what the endpoint's own response shape has always
    implied.

    The nightly path keeps using ``run_crystallization`` and awaiting it: it is
    already a background job with no request budget, and its caller wants the
    completed result.
    """
    sc = get_storage_client()
    report_id, is_new = await _reserve_report(sc, tenant_id, fleet_id, trigger)
    if not is_new:
        return report_id

    # Published, NOT ``track_task``-ed. A fire-and-forget asyncio task assumes the
    # process keeps scheduling it after the response is flushed, which a managed
    # runtime that allocates CPU per-request does not guarantee — the deployed
    # core-api has no ``--no-cpu-throttling`` in staging, and even where it does,
    # nothing keeps an instance alive for a multi-minute task once no request is in
    # flight. That failure mode is this bug again by another route: a report stuck
    # 'running', starved instead of crashed.
    #
    # The event bus is the existing answer to exactly this. Under
    # ``EVENT_BUS_BACKEND=pubsub`` (what the deployed services run) the run becomes
    # a queued delivery with retry and a DLQ. Under the in-process bus (OSS
    # standalone default) it is an asyncio task in this process — no better than
    # ``track_task``, and fine there, because a self-hosted container is not
    # subject to request-scoped CPU throttling.
    #
    # The staleness cutoff in ``report_find_running`` remains the backstop for the
    # case where the message is never delivered at all.
    try:
        await publish_crystallize_on_demand_request(
            tenant_id=tenant_id,
            report_id=str(report_id),
            fleet_id=fleet_id,
            auto_crystallize=auto_crystallize,
        )
    except BaseException:
        # The row is already 'running' and nothing will ever execute it, so
        # leaving it would block every later trigger for this tenant until the
        # staleness cutoff expires — an hour of "crystallization does nothing"
        # bought by one transient publish error. That is this bug's own shape,
        # reached through the fix for it.
        #
        # Marked terminal before re-raising, so the caller gets a report they can
        # inspect rather than only a 500, and the next trigger is free to start.
        logger.exception(
            "crystallize-on-demand publish failed for tenant %s; failing report %s",
            tenant_id,
            report_id,
        )
        try:
            await sc.update_report(
                str(report_id),
                {
                    "status": "failed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "summary": {"error": "could not queue the crystallization run"},
                },
            )
        except BaseException:
            # Same best-effort rule as the run's own guard: this must not replace
            # the publish failure the caller needs to see.
            logger.exception("Could not mark report %s failed", report_id)
        raise
    return report_id


async def execute_reserved_report(
    *,
    report_id: str,
    tenant_id: str,
    fleet_id: str | None,
    auto_crystallize: bool,
) -> None:
    """Run the analysis for a report the API already reserved.

    The consumer side of ``start_crystallization``. Separate from
    ``run_crystallization`` because it must NOT reserve: the row exists and the
    caller is already polling its id, so reserving another would leave that id
    unfinished forever — the wedge, reintroduced by the fix for it.

    IDEMPOTENT, because ``EventBus.subscribe`` says handlers must be and Pub/Sub
    is at-least-once. This op is not cheap to repeat — it is the multi-minute run
    that made moving off the request necessary in the first place — which also
    makes redelivery LIKELY rather than hypothetical: a run that outlives the
    subscription's ack deadline is redelivered while the first attempt is still
    going. Re-running would repeat every LLM call and overwrite a report that had
    already finished, so a row at a terminal status short-circuits.

    Deliberately NOT a lock: two deliveries can still overlap while the row is
    'running', because a status read cannot exclude an in-flight run. What this
    prevents is the case that actually corrupts a result — clobbering a
    'completed' or 'failed' report with a second full run.
    """
    sc = get_storage_client()
    # ``read=False``: this is a read-your-write across a delivery boundary. The
    # status being checked was written by the PREVIOUS delivery of this same
    # message, so a lagging replica can answer 'running' for a report that has
    # already finished — and then the whole multi-minute run happens again, which
    # is the exact duplication this check exists to prevent. Same class as H-02
    # (#812), where replica lag on a back-channel read-back dropped the trigger.
    existing = await sc.get_report(report_id, read=False)
    if existing is None:
        # Nothing to execute against. Reserving one here would hand back an id the
        # caller never asked for, so this drops — and the publisher's row going
        # missing is itself worth a line in the log.
        logger.warning("crystallize-on-demand: report %s no longer exists; dropping", report_id)
        return
    status = existing.get("status")
    if status in _TERMINAL_REPORT_STATUSES:
        logger.info(
            "crystallize-on-demand: report %s already %s; skipping redelivery",
            report_id,
            status,
        )
        return
    await _execute_crystallization(sc, report_id, tenant_id, fleet_id, auto_crystallize)


async def run_crystallization(
    tenant_id: str,
    fleet_id: str | None = None,
    trigger: str = "manual",
    auto_crystallize: bool = True,
) -> UUID:
    """Run a full memory crystallization for a tenant, INLINE. Returns the id.

    Kept awaiting for callers with no request budget that want the finished
    result — the nightly lifecycle trigger. Request handlers use
    ``start_crystallization``.
    """
    sc = get_storage_client()
    report_id, is_new = await _reserve_report(sc, tenant_id, fleet_id, trigger)
    if not is_new:
        return report_id
    await _execute_crystallization(sc, report_id, tenant_id, fleet_id, auto_crystallize)
    return report_id


async def _execute_crystallization(
    sc,
    report_id,
    tenant_id: str,
    fleet_id: str | None,
    auto_crystallize: bool,
) -> None:
    """Run the analysis for an ALREADY-RESERVED report row and give it a terminal
    status. Never leaves the row 'running'."""
    # H-07: everything from here to ``update_report`` runs inside a guard, because
    # the report row is ALREADY 'running' and ``find_running_report`` short-circuits
    # on it. An exception escaping here left the row 'running' forever, so every
    # later run — manual POST and the nightly lifecycle trigger alike — returned
    # that stale id and did nothing. Crystallization stayed off for the tenant
    # until someone hand-edited the row.
    #
    # ``except BaseException`` rather than ``Exception``: a CancelledError (the
    # nightly trigger's task being torn down, or a request deadline) wedges the row
    # exactly as thoroughly as a bug does, and is MORE likely in the scheduled
    # path. The row is marked and the exception re-raised unchanged, so callers and
    # cancellation semantics are untouched.
    try:
        t0 = time.monotonic()
        checks_failed = 0
        checks_total = 0

        # --- Hygiene checks ---
        hygiene: dict = {}
        for name, fn in [
            ("orphaned_entities", _check_orphaned_entities),
            ("near_duplicates", _check_near_duplicates),
            ("missing_embeddings", _check_missing_embeddings),
            ("expired_still_active", _check_expired_still_active),
            ("stale_memories", _check_stale_memories),
            ("short_content", _check_short_content),
            ("broken_entity_links", _check_broken_entity_links),
        ]:
            checks_total += 1
            try:
                hygiene[name] = await fn(tenant_id, fleet_id)
            except (SQLAlchemyError, ValueError, RuntimeError, Exception):
                logger.exception("Crystallizer check %s failed for tenant %s", name, tenant_id)
                hygiene[name] = {"error": True}
                checks_failed += 1

        # --- Health metrics ---
        health: dict = {}
        checks_total += 1
        try:
            health = await _compute_health(tenant_id, fleet_id)
        except (SQLAlchemyError, httpx.HTTPError, ValueError, RuntimeError):
            logger.exception("Crystallizer health computation failed for tenant %s", tenant_id)
            health = {"error": True}
            checks_failed += 1

        # --- Usage metrics ---
        usage: dict = {}
        checks_total += 1
        try:
            usage = await _compute_usage(tenant_id, fleet_id)
        except (SQLAlchemyError, httpx.HTTPError, ValueError, RuntimeError):
            logger.exception("Crystallizer usage computation failed for tenant %s", tenant_id)
            usage = {"error": True}
            checks_failed += 1

        # Remediating missing embeddings is deliberately NOT done here. Repair
        # lives in ``core_worker.backfill``, which pages
        # ``GET /memories/null-embedding-ids`` and publishes one
        # ``EMBED_REQUESTED`` per row, inheriting the consumer's per-tenant
        # concurrency, retry and DLQ wiring. Embedding inline inside a
        # crystallization run would instead draw from the process-wide embedding
        # gate that write traffic already oversubscribes, so a hygiene report
        # could stall the write path it is reporting on. This run's job is to
        # SURFACE the count (``_check_missing_embeddings``); acting on it is
        # ``python -m core_worker.cli backfill-embeddings --tenant-id X``.

        # --- Issues ---
        issues: list[dict] = []
        try:
            issues = _generate_issues(hygiene, health, usage)
        except (ValueError, RuntimeError, KeyError):
            logger.exception("Crystallizer issue generation failed for tenant %s", tenant_id)

        # --- Crystallization (auto-curate) ---
        crystallization: dict = {
            "enabled": auto_crystallize,
            "clusters_found": 0,
            "memories_crystallized": 0,
            "memories_archived": 0,
            "new_memories": 0,
            # Same shape as ``_run_crystallization``'s result, so
            # ``analysis_reports.crystallization`` does not depend on whether
            # auto-curate ran. A consumer reading ``duplicate_facts`` should not
            # have to know which branch produced the row.
            "duplicate_facts": 0,
            "failed_facts": 0,
        }
        if auto_crystallize:
            try:
                crystallization = await _run_crystallization(tenant_id, fleet_id, hygiene)
            except (SQLAlchemyError, httpx.HTTPError, ValueError, RuntimeError, HTTPException):
                # ``HTTPException`` added (H-07): it is not a ``ValueError``, so it
                # used to pass straight through this handler AND the per-fact one
                # inside the call, aborting the run before ``update_report``. The
                # per-fact handler is the real fix; this is the backstop for a 4xx
                # raised anywhere else under this call, and it preserves the
                # contract that a crystallization failure is NON-BLOCKING — the
                # report still completes with ``error: True``.
                logger.exception("Crystallization failed for tenant %s (non-blocking)", tenant_id)
                crystallization["error"] = True

        # Score: deduct points per severity
        critical = sum(1 for i in issues if i.get("severity") == "critical")
        warning = sum(1 for i in issues if i.get("severity") == "warning")
        info = sum(1 for i in issues if i.get("severity") == "info")
        overall_score = max(0, 100 - (critical * 20 + warning * 5 + info * 1))

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status = "failed" if checks_failed == checks_total else "completed"

        await sc.update_report(
            str(report_id),
            {
                "status": status,
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": elapsed_ms,
                "summary": {
                    "overall_score": overall_score,
                    "critical": critical,
                    "warning": warning,
                    "info": info,
                },
                "hygiene": hygiene,
                "health": health,
                "usage_data": usage,
                "issues": issues,
                "crystallization": crystallization,
            },
        )

        logger.info(
            "Crystallization complete for tenant=%s fleet=%s score=%d status=%s (%d ms, crystallized=%d->%d)",
            tenant_id,
            fleet_id,
            overall_score,
            status,
            elapsed_ms,
            crystallization.get("memories_archived", 0),
            crystallization.get("new_memories", 0),
        )
    except BaseException:
        logger.exception("Crystallization run failed for tenant %s; marking report failed", tenant_id)
        # Best-effort: if this write also fails there is nothing left to try, and
        # its exception must not replace the original one. The staleness cutoff in
        # ``report_find_running`` is the backstop for exactly that case.
        try:
            await sc.update_report(
                str(report_id),
                {
                    "status": "failed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
            )
        except BaseException:
            # ``BaseException``, matching the outer handler, and for the same
            # reason: if this write is itself cancelled — plausible during a
            # shutdown that cancelled the run in the first place — an
            # ``except Exception`` here would let that CancelledError escape and
            # REPLACE the exception being handled, which is precisely the
            # guarantee the comment above claims.
            logger.exception("Could not mark report %s failed", report_id)
        raise


# ---------------------------------------------------------------------------
# Crystallization logic
# ---------------------------------------------------------------------------


async def _run_crystallization(
    tenant_id: str,
    fleet_id: str | None,
    hygiene: dict,
) -> dict:
    """Identify clusters of noisy/redundant memories and crystallize them via LLM."""
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)

    sc = get_storage_client()

    result = {
        "enabled": True,
        "clusters_found": 0,
        "memories_crystallized": 0,
        "memories_archived": 0,
        "new_memories": 0,
        "clusters": [],
        # H-07: reported rather than merely logged. A run whose facts were all
        # rejected as duplicates now COMPLETES with ``new_memories: 0``, which is
        # indistinguishable from "found nothing to crystallize" unless the
        # rejections are counted somewhere the report can show.
        "duplicate_facts": 0,
        "failed_facts": 0,
    }

    # Collect candidate memory IDs from near-duplicate pairs
    dup_data = hygiene.get("near_duplicates", {})
    dup_pairs = dup_data.get("pairs", [])
    if not dup_pairs:
        return result

    # Build clusters from overlapping pairs
    clusters = _build_clusters(dup_pairs)
    clusters = [c for c in clusters if len(c) >= CRYSTALLIZER_MIN_CLUSTER_SIZE]
    result["clusters_found"] = len(clusters)

    if not clusters:
        return result

    # Limit total memories processed
    total_ids: list[UUID] = []
    selected_clusters: list[set[UUID]] = []
    for cluster in clusters:
        if len(total_ids) + len(cluster) > CRYSTALLIZER_MAX_BATCH_SIZE:
            break
        selected_clusters.append(cluster)
        total_ids.extend(cluster)

    if not total_ids:
        return result

    # Fetch full memory content for all candidates in one round-trip.
    # ``bulk_get_memories`` returns a list aligned to input ``ids`` with
    # ``None`` in the slots whose memory doesn't exist (or was soft-
    # deleted) — same per-row "skip missing" semantics the old loop's
    # ``if mem:`` gate provided, just with Nx fewer HTTPs (audit P5).
    memories_by_id: dict[UUID, dict] = {}
    bulk_rows = await sc.bulk_get_memories([str(mid) for mid in total_ids])
    for mid, mem in zip(total_ids, bulk_rows):
        if mem is not None:
            memories_by_id[mid] = mem

    # Process each cluster
    for cluster_ids in selected_clusters:
        cluster_memories = [memories_by_id[mid] for mid in cluster_ids if mid in memories_by_id]
        if len(cluster_memories) < CRYSTALLIZER_MIN_CLUSTER_SIZE:
            continue

        # Call LLM to crystallize
        extracted = await _crystallize_cluster(cluster_memories, config)
        if not extracted:
            continue

        # Create new crystallized memories via create_memory
        from core_api.schemas import MemoryCreate
        from core_api.services.memory_service import create_memory

        new_ids = []
        duplicate_facts = 0
        failed_facts = 0
        for fact in extracted:
            # CAURA-701/717: crystallizer bypasses the write pipeline (calls
            # create_memory directly, no MergeEnrichmentFields step), so its
            # LLM output cannot rely on the pipeline-level demotion of
            # deprecated types. Guard here instead — if residual training makes
            # the crystallizer LLM emit e.g. "semantic", coerce to the default
            # so the merger is enforced on this path too. Also demote
            # server-reserved types (outcome/rule/insight) since ``create_memory``
            # itself has no SERVER_RESERVED_MEMORY_TYPES check — that guard
            # lives at the route/MCP boundary, which the crystallizer bypasses.
            mt = fact.get("memory_type", "fact")
            if mt in CLASSIFIER_DEPRECATED_MEMORY_TYPES or mt in SERVER_RESERVED_MEMORY_TYPES:
                mt = DEFAULT_MEMORY_TYPE
            try:
                mem_out = await create_memory(
                    MemoryCreate(
                        tenant_id=tenant_id,
                        fleet_id=fleet_id,
                        agent_id="crystallizer",
                        content=fact["content"],
                        memory_type=mt,
                        weight=fact.get("weight", 0.7),
                        status="confirmed",
                        metadata={"crystallized_from": [str(m.get("id")) for m in cluster_memories]},
                    ),
                )
                new_ids.append(str(mem_out.id))
            except HTTPException as exc:
                # H-07: a 409 here is the EXPECTED outcome, not a fault, and it
                # used to escape this handler entirely — ``HTTPException`` is not
                # a ``ValueError``, so it propagated out of ``_run_crystallization``,
                # past the outer handler in ``run_crystallization`` (whose tuple
                # does not include it either), and aborted the run BEFORE
                # ``update_report`` — leaving the report row 'running' forever and
                # every later run short-circuiting on it. Permanently, until an
                # operator hand-edited the row.
                #
                # Expected by construction: a crystallized fact is a near-verbatim
                # merge of cluster members that are >=0.95 similar to each other
                # and still ACTIVE at this point (sources are archived only after
                # this loop), so ``create_memory``'s semantic gate fires on it. The
                # fake-LLM path returns a member's content VERBATIM, so the EXACT
                # content-hash gate fires too — that one does not even need
                # semantic dedup enabled.
                #
                # Non-409s are counted here as well rather than re-raised: this
                # loop's job is per-fact isolation, and a 422/500 on one fact is
                # no more reason to abandon the cluster than a 409 is. Both are
                # logged, and a run that creates nothing still completes and says
                # so, which is what makes the report honest instead of absent.
                if exc.status_code == 409:
                    duplicate_facts += 1
                    logger.info(
                        "Crystallized fact already exists; skipping",
                        extra={"tenant_id": tenant_id, "detail": str(exc.detail)},
                    )
                else:
                    failed_facts += 1
                    logger.warning(
                        "Crystallized fact rejected with %d: %s",
                        exc.status_code,
                        exc.detail,
                    )
            except (SQLAlchemyError, ValueError, GoogleAPIError):
                failed_facts += 1
                logger.exception("Failed to create crystallized memory")

        # Archive source memories via a single per-cluster batch HTTP
        # (audit P5). Preserves the prior shape: ``archived_ids`` lists
        # only the ids the storage layer actually flipped to
        # ``archived``. Rows the batch endpoint reports back in
        # ``skipped`` (CAS miss, soft-deleted, or nonexistent id) are
        # left out of the count, same as the per-row try/except path
        # used to drop on exception. The whole-batch try/except keeps
        # one cluster's archive failure from killing the rest of the
        # sweep — same isolation the per-row loop gave us, just at
        # cluster granularity (K HTTPs instead of K x M).
        archived_ids: list[str] = []
        cluster_ids_to_archive = [
            {"memory_id": str(mem.get("id")), "status": "archived"} for mem in cluster_memories
        ]
        try:
            batch_result = await sc.batch_update_status(
                {"updates": cluster_ids_to_archive}, tenant_id=tenant_id
            )
            skipped_set = set(batch_result.get("skipped") or [])
            for item in cluster_ids_to_archive:
                if item["memory_id"] not in skipped_set:
                    archived_ids.append(item["memory_id"])
            if skipped_set:
                # Surface the dropped ids so an operator can grep by
                # cluster — the cluster's source_ids appear in the
                # ``clusters`` result below, so combining both lists
                # locates the affected sweep cycle.
                logger.warning(
                    "Crystallizer archive batch skipped %d row(s): %s",
                    len(skipped_set),
                    sorted(skipped_set),
                )
        except Exception:
            logger.exception("Failed to archive %d-memory cluster (rolled back)", len(cluster_ids_to_archive))

        result["clusters"].append(
            {
                "source_count": len(cluster_memories),
                "source_ids": [str(m.get("id")) for m in cluster_memories][:MAX_AFFECTED_IDS],
                "new_count": len(new_ids),
                "new_ids": new_ids[:MAX_AFFECTED_IDS],
            }
        )
        result["memories_archived"] += len(archived_ids)
        result["new_memories"] += len(new_ids)
        result["duplicate_facts"] += duplicate_facts
        result["failed_facts"] += failed_facts

    result["memories_crystallized"] = result["memories_archived"]
    return result


def _build_clusters(pairs: list[dict]) -> list[set[UUID]]:
    """Build connected components from near-duplicate pairs (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for pair in pairs:
        union(pair["id1"], pair["id2"])

    groups: dict[str, set[UUID]] = {}
    all_ids = {p["id1"] for p in pairs} | {p["id2"] for p in pairs}
    for mid in all_ids:
        root = find(mid)
        groups.setdefault(root, set()).add(UUID(mid))

    return list(groups.values())


async def _crystallize_cluster(memories: list[dict], config) -> list[dict]:
    """Send a cluster of memories to the LLM for crystallization."""
    mem_texts = []
    for i, m in enumerate(memories, 1):
        mem_texts.append(
            f"[{i}] ({m.get('memory_type', 'fact')}, weight={m.get('weight', 0.5)}) {m.get('content', '')}"
        )
    prompt = CRYSTALLIZATION_PROMPT.format(memories="\n".join(mem_texts))

    async def _do_crystallize(llm) -> list[dict]:
        raw = await llm.complete_json(prompt)
        if not isinstance(raw, list):
            return []
        results = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            # CAURA-717: match the prompt's offered vocabulary — any type
            # outside ``MEMORY_TYPES_WRITE`` (server-reserved OR
            # classifier-deprecated) is coerced to the default here, so a stray
            # LLM completion cannot smuggle a reserved/deprecated slug past
            # the write pipeline that ``_run_crystallization`` bypasses.
            if item.get("memory_type") not in MEMORY_TYPES_WRITE:
                item["memory_type"] = DEFAULT_MEMORY_TYPE
            try:
                item["weight"] = max(0.0, min(1.0, float(item.get("weight", 0.7))))
            except (TypeError, ValueError):
                item["weight"] = 0.7
            results.append(item)
        return results

    return await call_with_fallback(
        primary_provider_name=config.enrichment_provider,
        call_fn=_do_crystallize,
        # An outage must yield NOTHING here, not a stand-in. The caller does
        # ``if not extracted: continue`` before it creates anything, so an empty
        # list skips the cluster untouched — see ``_crystallize_fake``.
        fake_fn=(
            (lambda: _crystallize_fake(memories))
            if deliberate_fake_provider(config.enrichment_provider)
            else _skip_crystallize
        ),
        tenant_config=config,
        service_label="crystallizer",
        timeout=30.0,
    )


def _skip_crystallize() -> list[dict]:
    """No-LLM crystallization: produce nothing, so the cluster is left alone.

    This is not cosmetic. The caller creates one memory per returned fact and then
    ARCHIVES every source memory in the cluster. With ``_crystallize_fake`` on the
    outage path — it returns a verbatim copy of the cluster's highest-weight memory
    — an LLM outage archived N memories and left one duplicate of one of them
    behind, having synthesised nothing. The other N-1 memories' content is not in
    the survivor, and ``archived`` is outside ``LIVE_MEMORY_STATUSES``.

    Returning ``[]`` takes the caller's existing ``if not extracted: continue``
    path, so nothing is created and nothing is archived. The cluster is still there
    to crystallize once a provider answers.
    """
    logger.warning("crystallizer: no LLM — cluster skipped, nothing archived")
    return []


def _crystallize_fake(memories: list[dict]) -> list[dict]:
    """Stand-in crystallization for an explicitly configured ``fake`` provider:
    pick the highest-weight memory from the cluster. TEST/DEV ONLY — the production
    fallback abstains via ``_skip_crystallize``, because this output gets persisted
    and its sources archived."""
    if not memories:
        return []
    best = max(memories, key=lambda m: m.get("weight", 0.5))
    return [
        {
            "content": best.get("content", ""),
            "memory_type": best.get("memory_type", "fact"),
            "weight": best.get("weight", 0.5),
        }
    ]


# ---------------------------------------------------------------------------
# Hygiene checks (now using storage client)
# ---------------------------------------------------------------------------


async def _check_orphaned_entities(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Entities with zero memory_entity_links."""
    sc = get_storage_client()
    rows = await sc.find_orphaned_entities(tenant_id)
    ids = [str(r.get("id")) for r in rows]
    return {
        "count": len(rows),
        "affected_ids": ids[:MAX_AFFECTED_IDS],
        "sample_names": [r.get("canonical_name") for r in rows[:10]],
    }


async def _check_near_duplicates(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Find near-duplicate memory pairs via batch ANN neighbor queries."""
    sc = get_storage_client()

    pairs: dict[tuple[str, str], float] = {}  # (id1, id2) -> similarity
    checked_ids: list[str] = []
    offset = 0

    while len(pairs) < CRYSTALLIZER_MAX_DEDUP_PAIRS:
        batch = await sc.check_near_duplicates(
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "batch_size": CRYSTALLIZER_DEDUP_BATCH_SIZE,
                "offset": offset,
            }
        )
        candidates = batch.get("candidates", [])
        if not candidates:
            break

        for cand in candidates:
            mem_id = cand["id"]
            embedding = cand["embedding"]
            checked_ids.append(mem_id)

            neighbors = await sc.find_neighbors_by_embedding(
                {
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "query_embedding": embedding,
                    "exclude_id": mem_id,
                    "threshold": CRYSTALLIZER_DEDUP_THRESHOLD,
                    "limit": CRYSTALLIZER_DEDUP_NEIGHBORS,
                }
            )

            for nb in neighbors:
                id1, id2 = sorted([mem_id, nb["id"]])
                pair_key = (id1, id2)
                if pair_key not in pairs and len(pairs) < CRYSTALLIZER_MAX_DEDUP_PAIRS:
                    pairs[pair_key] = nb["similarity"]

        offset += CRYSTALLIZER_DEDUP_BATCH_SIZE

    # Mark all processed memories as dedup-checked
    if checked_ids:
        await sc.mark_dedup_checked(checked_ids, tenant_id)

    pairs_list = [{"id1": k[0], "id2": k[1], "similarity": v} for k, v in pairs.items()]
    return {"count": len(pairs_list), "pairs": pairs_list}


async def _check_missing_embeddings(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories with no embedding vector.

    ``GET /memories/embedding-coverage`` returns exactly three keys —
    ``total_active``, ``missing_embeddings``, ``coverage_pct`` — so this reads
    ``missing_embeddings``. It previously read ``missing_count`` and
    ``missing_ids``, neither of which the endpoint has ever returned, so both
    ``.get()`` calls fell through to their defaults and the check reported
    ``count: 0`` for every tenant no matter how many rows were unembedded.

    No ``affected_ids`` is returned because the endpoint exposes only counts,
    not ids. ``_generate_issues`` defaults the field to ``[]``, so the finding
    keeps the same shape as its siblings. Populating it would mean extending
    the storage endpoint to select ids as well — worth doing only if something
    starts consuming them.
    """
    sc = get_storage_client()
    coverage = await sc.get_embedding_coverage(tenant_id, fleet_id)
    return {"count": coverage.get("missing_embeddings", 0)}


async def _check_expired_still_active(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories past their validity window but still marked active.

    ``/lifecycle-candidates`` returns each list as bare UUID **strings**, not
    row dicts, so these are used directly. Calling ``.get("id")`` on them
    raised ``AttributeError`` for any tenant that had even one expired
    memory, which ``run_crystallization`` swallowed into
    ``{"error": True}`` — so the check reported an error precisely when it
    had something to report, and nothing at all when it didn't.
    """
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    expired = candidates.get("expired_still_active", [])
    return {"count": len(expired), "affected_ids": [str(r) for r in expired][:MAX_AFFECTED_IDS]}


async def _check_stale_memories(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Old memories never recalled and with low weight.

    The endpoint's key is ``stale_low_weight``; this read ``stale_memories``,
    which it has never returned, so the check reported zero for every tenant.
    Values are bare UUID strings — see ``_check_expired_still_active``.
    """
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    stale = candidates.get("stale_low_weight", [])
    return {"count": len(stale), "affected_ids": [str(r) for r in stale][:MAX_AFFECTED_IDS]}


async def _check_short_content(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories with very short content (likely low value).

    The endpoint had no ``short_content`` key until now, so this reported zero
    for every tenant — and would have raised on ``r.get("id")`` the moment it
    didn't, since the values are bare UUID strings. See
    ``_check_expired_still_active``.
    """
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    short = candidates.get("short_content", [])
    return {"count": len(short), "affected_ids": [str(r) for r in short][:MAX_AFFECTED_IDS]}


async def _check_broken_entity_links(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Entity links pointing to soft-deleted memories."""
    sc = get_storage_client()
    rows = await sc.find_broken_entity_links(tenant_id)
    ids = [str(r.get("id")) for r in rows]
    return {"count": len(rows), "affected_ids": list(set(ids))[:MAX_AFFECTED_IDS]}


# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------


async def _compute_health(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    sc = get_storage_client()
    # The three storage reads are independent — fetch them concurrently rather
    # than paying three serial HTTP round-trips. (Entity coverage runs a
    # cross-table join that lives in the storage API now.)
    health, coverage, with_entities = await asyncio.gather(
        sc.get_memory_stats(tenant_id, fleet_id),
        sc.get_embedding_coverage(tenant_id, fleet_id),
        sc.get_entity_coverage(tenant_id, fleet_id),
    )
    total = health.get("total_memories", 0)
    health["embedding_coverage_pct"] = coverage.get("coverage_pct", 0.0)
    health["entity_coverage_pct"] = round(with_entities / total * 100, 1) if total > 0 else 0.0

    return health


# ---------------------------------------------------------------------------
# Usage metrics
# ---------------------------------------------------------------------------


async def _compute_usage(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    sc = get_storage_client()
    # Memory-table stats, type distribution, and audit usage are independent
    # storage reads — fetch them concurrently rather than serially.
    stats, type_dist, audit = await asyncio.gather(
        sc.get_memory_stats(tenant_id, fleet_id),
        sc.get_type_distribution(tenant_id, fleet_id),
        sc.get_audit_usage(tenant_id),
    )
    usage: dict = {
        "total_memories": stats.get("total_memories", 0),
        "type_distribution": type_dist,
    }

    # Agent activity + peak hours from audit_log (fetched above via storage API).
    # ``search_write_ratio`` (and its total_writes/total_searches) is intentionally
    # dropped — the ``usage_counters`` table does not exist in the OSS schema; the
    # keys are kept as 0/None for response-shape parity.
    usage["agent_activity"] = audit.get("agent_activity", [])
    usage["peak_hours"] = audit.get("peak_hours", [])
    usage["total_writes"] = 0
    usage["total_searches"] = 0
    usage["search_write_ratio"] = None

    return usage


# ---------------------------------------------------------------------------
# Issue generation
# ---------------------------------------------------------------------------


def _generate_issues(hygiene: dict, health: dict, usage: dict) -> list[dict]:
    """Examine metrics and produce a list of actionable issues."""
    issues: list[dict] = []

    def _add(
        severity: str,
        category: str,
        code: str,
        title: str,
        description: str,
        count: int = 0,
        affected_ids: list | None = None,
    ):
        issues.append(
            {
                "severity": severity,
                "category": category,
                "code": code,
                "title": title,
                "description": description,
                "count": count,
                "affected_ids": (affected_ids or [])[:MAX_AFFECTED_IDS],
            }
        )

    # --- Hygiene issues ---

    dup = hygiene.get("near_duplicates", {})
    if dup.get("count", 0) > 0:
        _add(
            "warning",
            "hygiene",
            "NEAR_DUPLICATES",
            "Near-duplicate memories detected",
            f"{dup['count']} memory pair(s) exceed {CRYSTALLIZER_DEDUP_THRESHOLD} cosine similarity.",
            count=dup["count"],
            affected_ids=dup.get("affected_ids"),
        )

    orphan = hygiene.get("orphaned_entities", {})
    if orphan.get("count", 0) > 0:
        _add(
            "info",
            "hygiene",
            "ORPHANED_ENTITIES",
            "Orphaned entities with no linked memories",
            f"{orphan['count']} entities have no memory links and may be stale.",
            count=orphan["count"],
            affected_ids=orphan.get("affected_ids"),
        )

    missing_emb = hygiene.get("missing_embeddings", {})
    if missing_emb.get("count", 0) > 0:
        _add(
            "warning",
            "hygiene",
            "MISSING_EMBEDDINGS",
            "Memories without embeddings",
            f"{missing_emb['count']} memories lack embedding vectors and cannot be found by semantic search.",
            count=missing_emb["count"],
            affected_ids=missing_emb.get("affected_ids"),
        )

    expired = hygiene.get("expired_still_active", {})
    if expired.get("count", 0) > 0:
        _add(
            "warning",
            "hygiene",
            "EXPIRED_STILL_ACTIVE",
            "Expired memories still marked active",
            f"{expired['count']} memories are past ts_valid_end but still have status=active.",
            count=expired["count"],
            affected_ids=expired.get("affected_ids"),
        )

    stale = hygiene.get("stale_memories", {})
    if stale.get("count", 0) > 0:
        _add(
            "info",
            "hygiene",
            "STALE_MEMORIES",
            "Stale memories with no recall activity",
            f"{stale['count']} memories older than {CRYSTALLIZER_STALE_DAYS} days have never been recalled "
            f"and have weight below {CRYSTALLIZER_STALE_MAX_WEIGHT}.",
            count=stale["count"],
            affected_ids=stale.get("affected_ids"),
        )

    short = hygiene.get("short_content", {})
    if short.get("count", 0) > 0:
        _add(
            "info",
            "hygiene",
            "SHORT_CONTENT",
            "Memories with very short content",
            f"{short['count']} memories have content shorter than {CRYSTALLIZER_SHORT_CONTENT_CHARS} characters.",
            count=short["count"],
            affected_ids=short.get("affected_ids"),
        )

    broken = hygiene.get("broken_entity_links", {})
    if broken.get("count", 0) > 0:
        _add(
            "warning",
            "hygiene",
            "BROKEN_ENTITY_LINKS",
            "Entity links pointing to deleted memories",
            f"{broken['count']} memory-entity links reference soft-deleted memories.",
            count=broken["count"],
            affected_ids=broken.get("affected_ids"),
        )

    # --- Health issues ---

    if not health.get("error"):
        total = health.get("total_memories", 0)

        if total > 0 and health.get("embedding_coverage_pct", 100) < CRYSTALLIZER_LOW_EMBEDDING_COVERAGE_PCT:
            _add(
                "critical",
                "health",
                "LOW_EMBEDDING_COVERAGE",
                "Low embedding coverage",
                f"Only {health['embedding_coverage_pct']}% of memories have embeddings "
                f"(threshold: {CRYSTALLIZER_LOW_EMBEDDING_COVERAGE_PCT}%).",
                count=total,
            )

        status_dist = health.get("status_distribution", {})
        pending = status_dist.get("pending", 0)
        if total > 0 and pending / total * 100 > CRYSTALLIZER_HIGH_PENDING_PCT:
            pct = round(pending / total * 100, 1)
            _add(
                "warning",
                "health",
                "HIGH_PENDING_RATIO",
                "High ratio of pending memories",
                f"{pct}% of memories are in pending status ({pending}/{total}).",
                count=pending,
            )

        pii = health.get("pii_count", 0)
        if pii >= CRYSTALLIZER_HIGH_PII_COUNT:
            _add(
                "warning",
                "health",
                "HIGH_PII_COUNT",
                "Significant PII-containing memories",
                f"{pii} memories flagged as containing PII. Review data handling policies.",
                count=pii,
            )

        contradiction_count = health.get("contradiction_count", 0)
        if contradiction_count > 0:
            _add(
                "info",
                "health",
                "CONTRADICTIONS_PRESENT",
                "Contradicted or outdated memories present",
                f"{contradiction_count} memories have status outdated or conflicted.",
                count=contradiction_count,
            )

    return issues
