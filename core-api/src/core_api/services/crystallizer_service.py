"""Memory Crystallizer engine — hygiene checks, health metrics, usage analysis, and memory crystallization."""

import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

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
    MEMORY_TYPES,
)
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)

MAX_AFFECTED_IDS = 20


# ---------------------------------------------------------------------------
# Crystallization LLM prompt
# ---------------------------------------------------------------------------

CRYSTALLIZATION_PROMPT = """\
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
- Assign a memory_type to each fact: one of "fact", "episode", "decision", "preference", "task", "semantic", "intention", "plan", "commitment", "action", "outcome", "cancellation", "rule"
- Assign a weight (0.0-1.0) based on importance

Input memories:
{memories}

Return ONLY a JSON array of objects (no markdown fences):
[{{"content": "...", "memory_type": "...", "weight": 0.0}}, ...]

If the input contains no meaningful information worth preserving, return an empty array: []
"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_crystallization(
    db: AsyncSession,
    tenant_id: str,
    fleet_id: str | None = None,
    trigger: str = "manual",
    auto_crystallize: bool = True,
) -> UUID:
    """Run a full memory crystallization for a tenant. Returns the report ID."""
    sc = get_storage_client()

    # Check for an already-running report for this tenant/fleet
    running = await sc.find_running_report(tenant_id, fleet_id, report_type="crystallization")
    if running:
        return running.get("id")

    # Create report row
    report = await sc.create_report(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "trigger": trigger,
            "status": "running",
            "report_type": "crystallization",
        }
    )
    report_id = report.get("id")

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
        health = await _compute_health(db, tenant_id, fleet_id)
    except (SQLAlchemyError, ValueError, RuntimeError):
        logger.exception("Crystallizer health computation failed for tenant %s", tenant_id)
        health = {"error": True}
        checks_failed += 1

    # --- Usage metrics ---
    usage: dict = {}
    checks_total += 1
    try:
        usage = await _compute_usage(db, tenant_id, fleet_id)
    except (SQLAlchemyError, ValueError, RuntimeError):
        logger.exception("Crystallizer usage computation failed for tenant %s", tenant_id)
        usage = {"error": True}
        checks_failed += 1

    # --- Remediate missing embeddings ---
    try:
        await _remediate_missing_embeddings(tenant_id, fleet_id)
    except (SQLAlchemyError, ValueError, RuntimeError):
        logger.exception("Embedding remediation failed for tenant %s (non-blocking)", tenant_id)

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
    }
    if auto_crystallize:
        try:
            crystallization = await _run_crystallization(db, tenant_id, fleet_id, hygiene)
        except (SQLAlchemyError, ValueError, RuntimeError):
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
    return report_id


# ---------------------------------------------------------------------------
# Embedding remediation
# ---------------------------------------------------------------------------


async def _remediate_missing_embeddings(
    tenant_id: str,
    fleet_id: str | None,
) -> None:
    """Re-embed memories that have NULL embeddings (e.g. from earlier failures)."""
    from common.embedding import get_embedding

    sc = get_storage_client()
    # Use the lifecycle_candidates endpoint which provides missing-embedding data
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    missing = candidates.get("missing_embeddings", [])
    if not missing:
        return

    patched = 0
    for item in missing:
        mem_id = item.get("id")
        content = item.get("content", "")
        embedding = await get_embedding(content)
        if embedding is None:
            continue
        await sc.update_embedding(str(mem_id), embedding)
        patched += 1

    if patched:
        logger.info(
            "Remediated %d/%d missing embeddings for tenant=%s fleet=%s",
            patched,
            len(missing),
            tenant_id,
            fleet_id,
        )


# ---------------------------------------------------------------------------
# Crystallization logic
# ---------------------------------------------------------------------------


async def _run_crystallization(
    db: AsyncSession,
    tenant_id: str,
    fleet_id: str | None,
    hygiene: dict,
) -> dict:
    """Identify clusters of noisy/redundant memories and crystallize them via LLM."""
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(db, tenant_id)

    sc = get_storage_client()

    result = {
        "enabled": True,
        "clusters_found": 0,
        "memories_crystallized": 0,
        "memories_archived": 0,
        "new_memories": 0,
        "clusters": [],
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
        for fact in extracted:
            try:
                mem_out = await create_memory(
                    db,
                    MemoryCreate(
                        tenant_id=tenant_id,
                        fleet_id=fleet_id,
                        agent_id="crystallizer",
                        content=fact["content"],
                        memory_type=fact.get("memory_type", "fact"),
                        weight=fact.get("weight", 0.7),
                        status="confirmed",
                        metadata={"crystallized_from": [str(m.get("id")) for m in cluster_memories]},
                    ),
                )
                new_ids.append(str(mem_out.id))
            except (SQLAlchemyError, ValueError, GoogleAPIError):
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
            batch_result = await sc.batch_update_status({"updates": cluster_ids_to_archive})
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
            if item.get("memory_type") not in MEMORY_TYPES:
                item["memory_type"] = "fact"
            try:
                item["weight"] = max(0.0, min(1.0, float(item.get("weight", 0.7))))
            except (TypeError, ValueError):
                item["weight"] = 0.7
            results.append(item)
        return results

    return await call_with_fallback(
        primary_provider_name=config.enrichment_provider,
        call_fn=_do_crystallize,
        fake_fn=lambda: _crystallize_fake(memories),
        tenant_config=config,
        service_label="crystallizer",
        timeout=30.0,
    )


def _crystallize_fake(memories: list[dict]) -> list[dict]:
    """Fake crystallization for testing: just pick the highest-weight memory from the cluster."""
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
        await sc.mark_dedup_checked(checked_ids)

    pairs_list = [{"id1": k[0], "id2": k[1], "similarity": v} for k, v in pairs.items()]
    return {"count": len(pairs_list), "pairs": pairs_list}


async def _check_missing_embeddings(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories with no embedding vector."""
    sc = get_storage_client()
    coverage = await sc.get_embedding_coverage(tenant_id, fleet_id)
    missing_count = coverage.get("missing_count", 0)
    missing_ids = coverage.get("missing_ids", [])
    return {"count": missing_count, "affected_ids": [str(i) for i in missing_ids][:MAX_AFFECTED_IDS]}


async def _check_expired_still_active(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories past their validity window but still marked active."""
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    expired = candidates.get("expired_still_active", [])
    ids = [str(r.get("id")) for r in expired]
    return {"count": len(expired), "affected_ids": ids[:MAX_AFFECTED_IDS]}


async def _check_stale_memories(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Old memories never recalled and with low weight."""
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    stale = candidates.get("stale_memories", [])
    ids = [str(r.get("id")) for r in stale]
    return {"count": len(stale), "affected_ids": ids[:MAX_AFFECTED_IDS]}


async def _check_short_content(
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    """Memories with very short content (likely low value)."""
    sc = get_storage_client()
    candidates = await sc.get_lifecycle_candidates(tenant_id)
    short = candidates.get("short_content", [])
    ids = [str(r.get("id")) for r in short]
    return {"count": len(short), "affected_ids": ids[:MAX_AFFECTED_IDS]}


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
    db: AsyncSession,
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    sc = get_storage_client()
    # Memory stats from storage API
    health = await sc.get_memory_stats(tenant_id, fleet_id)
    total = health.get("total_memories", 0)

    # Embedding coverage from storage API
    coverage = await sc.get_embedding_coverage(tenant_id, fleet_id)
    health["embedding_coverage_pct"] = coverage.get("coverage_pct", 0.0)

    # Entity extraction coverage (cross-table join -- stays as direct SQL for now)
    if db is not None:
        from core_api.repositories import scope_sql as _scope_sql

        scope, params = _scope_sql(tenant_id, fleet_id)
        r = await db.execute(
            text(f"""
            SELECT COUNT(DISTINCT mel.memory_id)
            FROM memory_entity_links mel
            JOIN memories m ON m.id = mel.memory_id
            WHERE {scope} AND m.deleted_at IS NULL
        """),
            params,
        )
        with_entities = r.scalar() or 0
        health["entity_coverage_pct"] = round(with_entities / total * 100, 1) if total > 0 else 0.0

    return health


# ---------------------------------------------------------------------------
# Usage metrics
# ---------------------------------------------------------------------------


async def _compute_usage(
    db: AsyncSession,
    tenant_id: str,
    fleet_id: str | None,
) -> dict:
    sc = get_storage_client()
    # Memory-table usage stats from storage API
    stats = await sc.get_memory_stats(tenant_id, fleet_id)
    type_dist = await sc.get_type_distribution(tenant_id, fleet_id)
    usage: dict = {
        "total_memories": stats.get("total_memories", 0),
        "type_distribution": type_dist,
    }

    # Agent activity from audit_log (cross-table -- stays as direct SQL)
    if db is not None:
        r = await db.execute(
            text("""
            SELECT a.agent_id,
                   COUNT(*) FILTER (WHERE a.action = 'create' AND a.resource_type = 'memory') AS writes,
                   COUNT(*) FILTER (WHERE a.action = 'search') AS searches
            FROM audit_log a
            WHERE a.tenant_id = :tenant_id
            GROUP BY a.agent_id
            ORDER BY writes DESC
            LIMIT 20
        """),
            {"tenant_id": tenant_id},
        )
        usage["agent_activity"] = [
            {"agent_id": row[0], "writes": row[1], "searches": row[2]} for row in r.all()
        ]

        # Search/write ratio from usage_counters (cross-table)
        r = await db.execute(
            text("""
            SELECT SUM(writes), SUM(searches)
            FROM usage_counters
            WHERE tenant_id = :tenant_id
        """),
            {"tenant_id": tenant_id},
        )
        urow = r.one_or_none()
        total_writes = urow[0] if urow else 0
        total_searches = urow[1] if urow else 0
        usage["total_writes"] = total_writes
        usage["total_searches"] = total_searches
        usage["search_write_ratio"] = round(total_searches / total_writes, 2) if total_writes > 0 else None

        # Peak hours from audit_log (cross-table)
        r = await db.execute(
            text("""
            SELECT EXTRACT(hour FROM a.created_at)::int AS hr, COUNT(*) AS cnt
            FROM audit_log a
            WHERE a.tenant_id = :tenant_id
            GROUP BY hr
            ORDER BY cnt DESC
            LIMIT 3
        """),
            {"tenant_id": tenant_id},
        )
        usage["peak_hours"] = [{"hour": row[0], "count": row[1]} for row in r.all()]
    else:
        usage["agent_activity"] = []
        usage["total_writes"] = 0
        usage["total_searches"] = 0
        usage["search_write_ratio"] = None
        usage["peak_hours"] = []

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
