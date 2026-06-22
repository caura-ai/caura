"""Insights service -- LLM-powered memory analysis with 6 focus modes.

Examines the memory store to surface contradictions, failure patterns,
stale knowledge, cross-agent divergence, emerging themes, and unexpected
vector-space clusters. Findings are persisted as insight-type memories.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    INSIGHTS_DISCOVER_CLUSTERS,
    INSIGHTS_DISCOVER_SAMPLE_SIZE,
    INSIGHTS_FOCUS_MODES,
    INSIGHTS_MAX_MEMORIES,
    INSIGHTS_TEMPERATURE,
)
from core_api.utils.sanitize import sanitize_content as _sanitize_content

logger = logging.getLogger(__name__)


@dataclass
class _DiscoverResult:
    """Heterogeneous return type for _query_discover — either clusters or flat memories."""

    is_clustered: bool
    data: list[dict]


_SCOPE_TO_VISIBILITY = {
    "agent": "scope_agent",
    "fleet": "scope_team",
    "all": "scope_org",
}


# -- Prompts -------------------------------------------------------------------

_PROMPT_CONTRADICTIONS = """\
You are a memory analyst specializing in contradiction detection.

Analyze these {count} memories for contradiction clusters. Identify what is \
contradicted, which memories conflict, which version is likely correct \
(consider recency, weight, and agent trust), and what should be done to \
resolve the conflict.

Look for:
- Direct factual contradictions (same entity, different values)
- Superseded memories that may still be recalled
- Status conflicts (e.g. "active" vs "conflicted")
- Temporal contradictions (events placed at incompatible times)

Memories:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "contradictions",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

_PROMPT_FAILURES = """\
You are a memory analyst specializing in failure pattern detection.

These {count} memories have low importance but were recalled by agents -- \
meaning agents may have acted on weak or unreliable information. Identify \
recurring failure patterns, common root causes, and memories that should be \
deprecated or flagged.

Look for:
- Memories with very low weight that were recalled frequently
- Patterns in the types of unreliable information
- Agents that consistently rely on weak memories
- Information that should have been superseded but wasn't

Memories:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "failures",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

_PROMPT_STALE = """\
You are a memory analyst specializing in knowledge freshness.

These {count} memories are likely outdated -- they haven't been recalled \
recently or have very low weight. Identify which are genuinely stale vs. \
rarely needed, and flag ones that could cause harm if recalled.

Look for:
- Memories about time-sensitive topics (deadlines, prices, versions)
- Knowledge that likely changed since creation
- Memories that were never recalled (possibly irrelevant from the start)
- Low-weight memories that could mislead if surfaced

Memories:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "stale",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

_PROMPT_DIVERGENCE = """\
You are a memory analyst specializing in cross-agent knowledge consistency.

These {count} memories come from different agents about the same entities. \
Identify where agents disagree, which agent's perspective is more credible, \
and whether the divergence indicates a real disagreement or different contexts.

Look for:
- Same entity described differently by different agents
- Conflicting conclusions or assessments
- Different levels of detail or confidence
- Cases where divergence reveals complementary rather than conflicting views

Memories:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "divergence",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

_PROMPT_PATTERNS = """\
You are a memory analyst specializing in trend and pattern recognition.

Analyze these {count} recent memories for emerging themes, trends, and \
patterns. What topics are getting more attention? What decisions are being \
made? Are there any concerning patterns?

Look for:
- Recurring topics or entities across multiple memories
- Shifts in focus or priority over time
- Decision patterns and their outcomes
- Gaps in knowledge coverage

Memories:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "patterns",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""

_PROMPT_DISCOVER = """\
You are a memory analyst specializing in knowledge topology.

These are {count} memories organized into natural clusters discovered in the \
embedding vector space. For each cluster, what is the underlying theme? Are \
any clusters surprising? Are there gaps in knowledge between clusters?

Look for:
- Unexpected groupings that reveal hidden connections
- Clusters with high weight variance (inconsistent confidence)
- Cross-agent clusters (same topic, multiple agents)
- Missing clusters (topics you'd expect but don't see)

Clusters:
{memories}

Respond with JSON:
{{
  "findings": [
    {{
      "type": "discover",
      "title": "short headline (max 80 chars)",
      "description": "2-3 sentence explanation",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"],
      "recommendation": "actionable next step"
    }}
  ],
  "summary": "one paragraph overview"
}}"""


# -- Scope helpers -------------------------------------------------------------


def _scope_filters(tenant_id, fleet_id, agent_id, scope):
    """Validate the scope invariant and return the active scope markers.

    Fix 2 Ph5b: the analytic reads now build their WHERE clauses
    server-side (``PostgresService._insights_scope_filters`` ports the same
    base/agent/fleet/all logic VERBATIM). This thin helper survives as the
    client-side guard for the ``scope='fleet'`` invariant — and so the same
    ``ValueError`` is raised at the service boundary the data-layer raises —
    returning the list of *active* scope markers so the count semantics the
    callers/tests rely on are preserved (base ``tenant_id`` + ``deleted_at``
    always; ``agent_id`` and optional ``fleet_id`` under ``agent``; ``fleet_id``
    under ``fleet``; nothing extra under ``all``).
    """
    markers = ["tenant_id", "deleted_at"]
    if scope == "agent":
        markers.append("agent_id")
        if fleet_id:
            markers.append("fleet_id")
    elif scope == "fleet":
        if not fleet_id:
            raise ValueError("fleet_id is required when scope is 'fleet'")
        markers.append("fleet_id")
    # scope == "all": tenant-wide, no additional filters
    return markers


# -- Query functions (one per focus) -------------------------------------------
#
# Fix 2 Ph5b: each ``_query_*`` now routes its analytic read through
# core-storage-api (``sc.insights_*``); the source ORM SQL was ported VERBATIM
# into ``PostgresService.insights_query_*``. The leading ``db`` arg is retained
# (ignored) so the dispatch shape — ``query_fn(db, tenant_id, fleet_id,
# agent_id, scope)`` — and the MCP-tool ``_QUERY_DISPATCH`` patch points stay
# unchanged. ``_scope_filters`` runs first to raise the ``fleet`` invariant
# client-side before the round-trip.


async def _query_contradictions(db, tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch memories that supersede others, are conflicted, or share entities with divergent values."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_contradictions(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )


async def _query_failures(db, tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch low-weight memories that were recalled (agents acted on weak info)."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_failures(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )


async def _query_stale(db, tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch memories that are likely outdated based on age and recall activity."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    # Age thresholds computed on the caller's clock and bound server-side.
    now = datetime.now(UTC)
    thirty_days_ago = now - timedelta(days=30)
    fourteen_days_ago = now - timedelta(days=14)
    sc = get_storage_client()
    return await sc.insights_query_stale(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        thirty_days_ago=thirty_days_ago,
        fourteen_days_ago=fourteen_days_ago,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )


async def _query_divergence(db, tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch memories where multiple agents reference the same entities differently."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_divergence(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )


async def _query_patterns(db, tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch recent active memories for trend/pattern analysis."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_patterns(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )


def _numpy_kmeans(data, k, max_iters=20):
    """Simple k-means clustering using only numpy."""
    import numpy as np

    n = data.shape[0]
    # Initialize centroids via random sampling
    rng = np.random.default_rng(42)  # deterministic for reproducibility
    indices = rng.choice(n, size=k, replace=False)
    centroids = data[indices].copy()

    # Initialize to -1 (sentinel) so the convergence check on the first
    # iteration doesn't false-positive when all points happen to be assigned
    # to cluster 0.
    labels = np.full(n, -1, dtype=np.int32)
    for _ in range(max_iters):
        # Assign each point to nearest centroid (squared-distance, no huge intermediate)
        data_sq = np.sum(data**2, axis=1, keepdims=True)
        cent_sq = np.sum(centroids**2, axis=1)[None, :]
        dists = data_sq + cent_sq - 2.0 * (data @ centroids.T)
        new_labels = np.argmin(dists, axis=1).astype(np.int32)

        if np.array_equal(labels, new_labels):
            break
        labels = new_labels

        # Update centroids
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = data[mask].mean(axis=0)
            else:
                centroids[j] = data[rng.integers(n)]

    return labels, centroids


async def _query_discover(db, tenant_id, fleet_id, agent_id, scope) -> _DiscoverResult:
    """Sample memories with embeddings and cluster them in vector space.

    Fix 2 Ph5b: only the row sample routes through storage
    (``sc.insights_discover_sample`` — rows come back as dicts INCLUDING the
    raw ``embedding``); the numpy k-means + cluster-build stay client-side.
    """
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    # ``rows`` are plain dicts (``_insights_rows_to_dicts(..., include_embedding=True)``)
    # — already the ``_rows_to_dicts`` shape the formatter consumes, with the
    # raw embedding vector for clustering.
    rows = await sc.insights_discover_sample(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
    )

    def _strip_embeddings(dicts: list[dict]) -> list[dict]:
        # The prompt formatter never reads ``embedding``; drop it from the
        # non-clustered fallback shape so it matches the pre-Ph5b output.
        return [{k: v for k, v in d.items() if k != "embedding"} for d in dicts]

    if len(rows) < 10:
        # Not enough data for meaningful clustering
        return _DiscoverResult(is_clustered=False, data=_strip_embeddings(rows))

    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy not available, falling back to patterns mode for discover")
        return _DiscoverResult(is_clustered=False, data=_strip_embeddings(rows[:INSIGHTS_MAX_MEMORIES]))

    # Extract embeddings into numpy array
    embeddings = np.array([r["embedding"] for r in rows], dtype=np.float32)
    n_clusters = min(INSIGHTS_DISCOVER_CLUSTERS, len(rows) // 5)
    n_clusters = max(2, n_clusters)

    # Simple numpy k-means (avoids sklearn dependency)
    labels, centroids = _numpy_kmeans(embeddings, n_clusters, max_iters=20)

    # Build cluster summaries with representative memories
    clusters = []
    for k in range(n_clusters):
        mask = labels == k
        cluster_indices = np.where(mask)[0]
        if len(cluster_indices) == 0:
            continue

        cluster_embeddings = embeddings[cluster_indices]
        centroid = centroids[k]

        # Find 3 closest to centroid
        dists = np.linalg.norm(cluster_embeddings - centroid, axis=1)
        closest_idx = np.argsort(dists)[:3]
        representatives = [rows[cluster_indices[i]] for i in closest_idx]

        # Compute cluster stats
        cluster_rows = [rows[i] for i in cluster_indices]
        weights = [r["weight"] for r in cluster_rows]
        agents = {r["agent_id"] for r in cluster_rows}
        types: dict[str, int] = {}
        for r in cluster_rows:
            types[r["memory_type"]] = types.get(r["memory_type"], 0) + 1

        clusters.append(
            {
                "cluster_id": k,
                "size": len(cluster_indices),
                "weight_mean": float(np.mean(weights)),
                "weight_std": float(np.std(weights)),
                "agent_count": len(agents),
                "agents": sorted(agents),
                "type_distribution": types,
                "representatives": _strip_embeddings(representatives),
            }
        )

    # Return cluster data (will be formatted differently by _format_clusters_for_analysis)
    return _DiscoverResult(is_clustered=True, data=clusters)


_QUERY_DISPATCH = {
    "contradictions": _query_contradictions,
    "failures": _query_failures,
    "stale": _query_stale,
    "divergence": _query_divergence,
    "patterns": _query_patterns,
    "discover": _query_discover,
}

_PROMPT_DISPATCH = {
    "contradictions": _PROMPT_CONTRADICTIONS,
    "failures": _PROMPT_FAILURES,
    "stale": _PROMPT_STALE,
    "divergence": _PROMPT_DIVERGENCE,
    "patterns": _PROMPT_PATTERNS,
    "discover": _PROMPT_DISCOVER,
}


# -- Formatting ----------------------------------------------------------------


def _format_memories_for_analysis(memories: list[dict]) -> tuple[str, set[str]]:
    """Format memory dicts into numbered lines for LLM consumption.

    Returns (text, shown_ids) so downstream validation of the LLM's
    related_memory_ids stays in sync with what was actually rendered
    into the prompt (even if entries are skipped or truncated here).
    """
    lines = []
    shown_ids: set[str] = set()
    for i, m in enumerate(memories, 1):
        meta_parts = []
        if m.get("ts_valid_start"):
            meta_parts.append(f"[{m['ts_valid_start'][:10]}]")
        if m.get("title"):
            meta_parts.append(f"— {_sanitize_content(m['title'], max_len=120)}")
        if m.get("status") and m["status"] != "active":
            meta_parts.append(f"[status: {m['status']}]")
        meta_parts.append(f"[weight: {m.get('weight', 0.5):.2f}]")
        meta_parts.append(f"[agent: {_sanitize_content(m.get('agent_id', '?'), max_len=100)}]")
        if m.get("recall_count", 0) > 0:
            meta_parts.append(f"[recalls: {m['recall_count']}]")
        if m.get("supersedes_id"):
            meta_parts.append(f"[supersedes: {m['supersedes_id']}]")
        meta = " ".join(meta_parts)
        content = _sanitize_content(m.get("content", ""))
        lines.append(f"{i}. (id:{m['id']}) [{m.get('memory_type', 'fact')}] {meta}: {content}")
        if m.get("id"):
            shown_ids.add(str(m["id"]))
    return "\n".join(lines), shown_ids


def _format_clusters_for_analysis(clusters: list[dict]) -> tuple[str, set[str]]:
    """Format cluster summaries for the discover-mode LLM prompt.

    Returns (text, shown_ids) — only representative IDs actually rendered
    into the prompt are included, keeping hallucination-filter accurate.
    """
    lines = []
    shown_ids: set[str] = set()
    for c in clusters:
        lines.append(f"--- Cluster {c['cluster_id']} ({c['size']} memories) ---")
        lines.append(f"  Weight: mean={c['weight_mean']:.2f}, std={c['weight_std']:.2f}")
        safe_agents = [_sanitize_content(a, max_len=100) for a in c["agents"]]
        lines.append(f"  Agents: {', '.join(safe_agents)} ({c['agent_count']} unique)")
        lines.append(f"  Types: {c['type_distribution']}")
        lines.append("  Representative memories:")
        for r in c.get("representatives", []):
            title = _sanitize_content(r.get("title", "untitled"), max_len=120)
            content = _sanitize_content(r.get("content", ""), max_len=200)
            lines.append(f"    - (id:{r['id']}) [{r.get('memory_type', 'fact')}] {title}: {content}")
            if r.get("id"):
                shown_ids.add(str(r["id"]))
        lines.append("")
    return "\n".join(lines), shown_ids


# -- LLM ----------------------------------------------------------------------


async def _run_llm_analysis(prompt: str, config) -> dict:
    """Send the analysis prompt to the configured LLM provider."""
    from core_api.providers._retry import call_with_fallback

    async def _do_analysis(llm) -> dict:
        return await llm.complete_json(prompt, temperature=INSIGHTS_TEMPERATURE)

    return await call_with_fallback(
        primary_provider_name=config.enrichment_provider,
        call_fn=_do_analysis,
        fake_fn=lambda: _fake_insights(),
        tenant_config=config,
        service_label="insights",
        model_override=config.enrichment_model,
    )


def _fake_insights() -> dict:
    """Return placeholder findings for the fake/test provider."""
    return {
        "findings": [
            {
                "type": "patterns",
                "title": "Fake insight for testing",
                "description": "This is a placeholder finding generated by the fake provider.",
                "confidence": 0.5,
                "related_memory_ids": [],
                "recommendation": "No action needed (fake provider).",
            }
        ],
        "summary": "Fake analysis complete.",
    }


# -- Persist -------------------------------------------------------------------


async def _persist_findings(
    db: AsyncSession | None,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    focus: str,
    scope: str,
    findings: list[dict],
) -> list[str | None]:
    """Create insight-type memories for each finding.

    Supersedes previous active insights with the same focus+agent by
    transitioning them to 'outdated', preventing duplicate pile-up on re-runs.

    Fix 2 Ph5b storage-routing note (narrow widening)
    -------------------------------------------------
    The prior-supersede and the total-failure restore now go through
    core-storage-api (``sc.insights_supersede_priors`` /
    ``insights_restore_priors``), each its OWN committed transaction
    storage-side, rather than sharing this caller's (now ``None``) session.
    That widens the original same-session atomicity: the priors are
    committed-outdated BEFORE the bulk-create runs, and ``create_memories_bulk``
    is itself separately storage-committed. The ordering and the safety net
    are preserved — if every finding fails to persist, the restore call flips
    the priors back to ``active`` — but a crash strictly between the
    supersede-commit and the bulk-create would leave the priors outdated with
    no replacement (re-runnable: a subsequent pass regenerates them). This is
    acceptable for insights: priors are advisory analysis artifacts, not
    source-of-truth memories.
    """
    # Supersede existing active insights for this focus BEFORE creating new ones.
    from uuid import uuid4

    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
    from core_api.services.memory_service import create_memories_bulk

    sc = get_storage_client()

    # Transition prior insights for this focus/scope/fleet to "outdated" BEFORE
    # creating new ones. This prevents semantic-dedup in create_memory from
    # matching against the prior insight (which has near-identical content
    # template) and failing the new inserts with 409. We skip this step when
    # there are no findings to persist — outdating priors would leave the user
    # with nothing active. The select + UPDATE run atomically in ONE
    # storage-side transaction (``insights_supersede_priors``).
    prior_ids: list[str] = []
    if findings:
        try:
            result = await sc.insights_supersede_priors(
                tenant_id=tenant_id,
                agent_id=agent_id,
                focus=focus,
                scope=scope,
                fleet_id=fleet_id,
            )
            prior_ids = list(result.get("prior_ids", []))
            if prior_ids:
                logger.info(
                    "Superseded %d prior %s insights for agent=%s",
                    result.get("outdated_count", len(prior_ids)),
                    focus,
                    agent_id,
                )
        except httpx.HTTPStatusError:
            # A 4xx from the supersede endpoint is a code/contract bug (e.g. a
            # bad tenant_id / focus), not a transient failure — surface it
            # rather than silently returning empty insights with a 200.
            raise
        except Exception:
            logger.warning("Failed to supersede prior insights; skipping persist", exc_info=True)
            return [None] * len(findings)

    # Empty-findings short-circuit: ``BulkMemoryCreate.items`` enforces
    # min_length=1, and the prior-supersede block above already returned
    # `[None] * len(findings)` for the failure path. An empty findings
    # list reaches here only when no priors existed either — return
    # straight away without touching the bulk path.
    if not findings:
        return []

    # Build one ``BulkMemoryItem`` per finding so the persist runs as a
    # single ``create_memories_bulk`` call rather than N serial
    # ``create_memory`` round-trips each in their own savepoint (audit
    # finding #29). Per-item error isolation is preserved by the bulk
    # contract — failed rows surface as ``BulkItemResult(status="error")``
    # and become ``None`` in the returned ``insight_ids`` (same shape as
    # the prior per-savepoint exception path).
    #
    # write_mode behaviour change vs the pre-#29 path
    # -----------------------------------------------
    # The previous serial path passed ``write_mode="strong"`` on every
    # ``MemoryCreate``. ``BulkMemoryItem`` carries no ``write_mode``
    # field, and ``create_memories_bulk`` doesn't pick the strong vs fast
    # pipeline per item — so the bulk path effectively drops the strong
    # mode override. The only behavioural delta between the strong and
    # fast pipelines is the inline ``CheckSemanticDuplicate`` step (see
    # ``core_api/pipeline/compositions/write.py``); everything else
    # (embed, enrich, exact-dedup, write, schedule background tasks) is
    # identical. The post-write fire-and-forget tasks — entity extraction,
    # async contradiction detection, deferred enrichment — are still
    # scheduled per memory by the bulk path's ``ScheduleBackgroundTasks``-
    # equivalent loop, so contradiction detection coverage is intact.
    #
    # Why dropping inline semantic dedup is acceptable for insights
    # specifically: the supersede-priors block above ALREADY transitions
    # any prior active insight for this ``insight_focus`` + ``scope`` +
    # ``agent_id`` to ``outdated`` before this persist runs. That handles
    # the cross-run "same insight regenerated" dedup case at the
    # type-aware level that matters for insights. Inline semantic dedup
    # would compare each finding against EVERY memory in the tenant
    # (not just insights), which risks blocking a genuinely-novel
    # insight whose content happens to look semantically similar to an
    # unrelated fact. Net: cheaper persist AND fewer false-positive
    # rejections.
    titles: list[str] = []
    items: list[BulkMemoryItem] = []
    for finding in findings:
        title = str(finding.get("title", "Untitled insight"))[:80]
        titles.append(title)
        description = str(finding.get("description", ""))[:1000]
        recommendation = str(finding.get("recommendation", ""))[:500]
        confidence = max(0.0, min(1.0, float(finding.get("confidence", 0.5))))
        related_ids = finding.get("related_memory_ids", [])

        content = f"[Insight/{finding.get('type', focus)}] {title}: {description}"
        if recommendation:
            content += f" Recommendation: {recommendation}"

        # ``BulkMemoryItem`` has no ``title`` field — the title is encoded
        # in ``content`` as "[Insight/{type}] {title}: {description}".
        items.append(
            BulkMemoryItem(
                memory_type="insight",
                content=content,
                weight=confidence,
                metadata={
                    "insight_focus": focus,
                    "insight_scope": scope,
                    "insight_type": finding.get("type", focus),
                    "related_memory_ids": [str(rid) for rid in related_ids],
                    "recommendation": recommendation,
                    "confidence": confidence,
                },
            )
        )

    bulk_data = BulkMemoryCreate(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        items=items,
        visibility=_SCOPE_TO_VISIBILITY.get(scope, "scope_team"),
    )
    # Per-attempt id is required by the bulk contract for idempotent
    # retries; insights persist is called from one place per
    # generate_insights invocation, so a fresh uuid4 here is the right
    # granularity (a retry would be a fresh generate_insights call with
    # a new attempt id anyway).
    bulk_attempt_id = f"insights:{uuid4()}"

    insight_ids: list[str | None] = []
    try:
        response = await create_memories_bulk(db, bulk_data, bulk_attempt_id=bulk_attempt_id)
    except Exception:
        logger.exception("Bulk persist of insight findings failed entirely")
        insight_ids = [None] * len(findings)
    else:
        # Bulk contract: ``results`` is aligned to input order, one
        # entry per item. ``id`` is set for ``created`` /
        # ``duplicate_attempt`` / ``duplicate_content``; absent for
        # ``error``.
        by_index = {r.index: r for r in response.results}
        for i, finding_title in enumerate(titles):
            r = by_index.get(i)
            if r is None or r.id is None:
                if r is not None and r.error:
                    logger.warning(
                        "Failed to persist insight finding %s: %s",
                        finding_title,
                        r.error,
                    )
                else:
                    logger.warning(
                        "Insight finding %s missing from bulk response",
                        finding_title,
                    )
                insight_ids.append(None)
            else:
                insight_ids.append(str(r.id))

    # Safety net: if every finding failed to persist, restore the priors we
    # pre-emptively outdated so the user isn't left with nothing active.
    if prior_ids and insight_ids and all(iid is None for iid in insight_ids):
        try:
            restore = await sc.insights_restore_priors(tenant_id=tenant_id, prior_ids=prior_ids)
            logger.warning(
                "All %d insight findings failed to persist; restored %d prior insights to active",
                len(findings),
                restore.get("restored", 0),
            )
        except httpx.HTTPStatusError:
            raise  # 4xx = code bug; don't bury it in the best-effort restore
        except Exception:
            logger.warning("Failed to restore prior insights after total failure", exc_info=True)

    return insight_ids


def _to_float(val, default: float = 0.5) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# -- Public API ----------------------------------------------------------------


async def synthesize_insights(
    memories_or_clusters: list,
    is_clustered: bool,
    config,
    *,
    focus: str,
    scope: str,
) -> dict:
    """LLM-only analysis step. No DB access.

    Audit finding P3: ``memclaw_insights`` previously held its
    ``_mcp_session()`` open across the multi-second ``_run_llm_analysis``
    round-trip, pinning a pooled DB connection. This helper takes the
    already-queried memories + resolved tenant config and produces the
    same intermediate shape the legacy ``generate_insights`` body
    produced in steps 3-5, so the MCP tool can exit the session block
    before invoking it.

    Returns
    -------
    dict with:
      - ``findings``: list of sanitized finding dicts
      - ``summary``: LLM-emitted overall summary string
      - ``memories_analyzed``: count of memories that fed the prompt
    """
    prompt_template = _PROMPT_DISPATCH[focus]
    if is_clustered:
        memories_text, shown_ids = _format_clusters_for_analysis(memories_or_clusters)
        count = sum(c.get("size", 0) for c in memories_or_clusters)
    else:
        memories_text, shown_ids = _format_memories_for_analysis(memories_or_clusters)
        count = len(memories_or_clusters)
        if focus == "discover":
            prompt_template = _PROMPT_DISPATCH["patterns"]

    # ``str.format`` inserts the substituted ``memories`` value literally (it
    # never re-scans it for fields), so it must NOT be brace-escaped — escaping
    # would corrupt the Python dict reprs (cluster mode) and any user-controlled
    # {...} strings. A substituted value never raises KeyError.
    prompt = prompt_template.format(memories=memories_text, count=count)

    analysis = await _run_llm_analysis(prompt, config)

    findings = analysis.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    sanitized = []
    total_dropped = 0
    findings_with_drops = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        raw_related = [str(rid) for rid in f.get("related_memory_ids", []) if rid]
        kept_related = [rid for rid in raw_related if rid in shown_ids]
        dropped = len(raw_related) - len(kept_related)
        if dropped > 0:
            total_dropped += dropped
            findings_with_drops += 1
        sanitized.append(
            {
                "type": str(f.get("type", focus))[:50],
                "title": str(f.get("title", "Untitled"))[:80],
                "description": str(f.get("description", "")),
                "confidence": max(0.0, min(1.0, _to_float(f.get("confidence", 0.5)))),
                "related_memory_ids": kept_related,
                "recommendation": str(f.get("recommendation", "")),
            }
        )
    if total_dropped > 0:
        logger.info(
            "insights: dropped %d hallucinated related_memory_ids across %d findings (focus=%s, scope=%s)",
            total_dropped,
            findings_with_drops,
            focus,
            scope,
        )

    return {
        "findings": sanitized,
        "summary": analysis.get("summary", ""),
        "memories_analyzed": count,
    }


async def generate_insights(
    db: AsyncSession | None,
    tenant_id: str,
    focus: str,
    scope: str = "agent",
    fleet_id: str | None = None,
    agent_id: str = "mcp-agent",
) -> dict:
    """Run an LLM reasoning pass over a targeted memory subset and persist findings.

    Parameters
    ----------
    db : AsyncSession | None
        Retained for signature back-compat; ignored. Fix 2 Ph5b routes all
        DB access through core-storage-api, so callers pass ``None``.
    tenant_id : str
        Tenant identifier.
    focus : str
        One of INSIGHTS_FOCUS_MODES: contradictions, failures, stale,
        divergence, patterns, discover.
    scope : str
        "agent", "fleet", or "all".
    fleet_id : str | None
        Required when scope is "fleet".
    agent_id : str
        Agent identifier, defaults to "mcp-agent".

    Returns
    -------
    dict
        Analysis results including findings, summary, persisted insight IDs,
        and timing information.
    """
    t0 = time.perf_counter()

    if focus not in INSIGHTS_FOCUS_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid focus '{focus}'. Must be one of: {', '.join(INSIGHTS_FOCUS_MODES)}",
        )
    if scope not in ("agent", "fleet", "all"):
        raise HTTPException(status_code=422, detail=f"Invalid scope '{scope}'. Must be: agent, fleet, all")
    if scope == "fleet" and not fleet_id:
        raise HTTPException(
            status_code=422,
            detail="fleet_id is required when scope is 'fleet'.",
        )
    if focus == "divergence" and scope == "agent":
        raise HTTPException(
            status_code=422,
            detail="Focus 'divergence' requires scope='fleet' or scope='all' to compare across agents.",
        )

    # 1. Query memories based on focus
    query_fn = _QUERY_DISPATCH[focus]
    memories_or_clusters = await query_fn(db, tenant_id, fleet_id, agent_id, scope)

    if focus == "discover" and isinstance(memories_or_clusters, _DiscoverResult):
        is_clustered = memories_or_clusters.is_clustered
        memories_or_clusters = memories_or_clusters.data
    else:
        is_clustered = False

    if not memories_or_clusters:
        return {
            "focus": focus,
            "scope": scope,
            "memories_analyzed": 0,
            "findings": [],
            "summary": "No relevant memories found for this analysis.",
            "insight_memory_ids": [],
            "insights_ms": int((time.perf_counter() - t0) * 1000),
        }

    # 2. Resolve tenant config for LLM provider
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(db, tenant_id)

    # 3-5. LLM analysis (no DB). Delegated to ``synthesize_insights`` so
    # MCP callers that want to release their session before the LLM
    # round-trip can do so independently (see ``memclaw_insights``).
    synth = await synthesize_insights(
        memories_or_clusters,
        is_clustered,
        config,
        focus=focus,
        scope=scope,
    )
    findings = synth["findings"]

    # 6. Persist findings as insight memories. Fix 2 Ph5b: the supersede,
    # bulk-create and restore are each storage-committed independently — there
    # is no caller-side transaction to commit (``db`` is now None on the
    # storage-routed paths).
    insight_ids = await _persist_findings(db, tenant_id, agent_id, fleet_id, focus, scope, findings)

    return {
        "focus": focus,
        "scope": scope,
        "memories_analyzed": synth["memories_analyzed"],
        "findings": [{**f, "insight_memory_id": mid} for f, mid in zip(findings, insight_ids)],
        "summary": synth["summary"],
        "insight_memory_ids": [mid for mid in insight_ids if mid],
        "insights_ms": int((time.perf_counter() - t0) * 1000),
    }
