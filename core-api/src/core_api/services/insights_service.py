"""Insights service -- LLM-powered memory analysis with 6 focus modes.

Examines the memory store to surface contradictions, failure patterns,
stale knowledge, cross-agent divergence, emerging themes, and unexpected
vector-space clusters. Findings are persisted as insight-type memories.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    INSIGHTS_DISCOVER_CLUSTERS,
    INSIGHTS_DISCOVER_SAMPLE_SIZE,
    INSIGHTS_DISCOVER_WINDOW_DAYS,
    INSIGHTS_FAILURES_WINDOW_DAYS,
    INSIGHTS_FOCUS_MODES,
    INSIGHTS_MAX_MEMORIES,
    INSIGHTS_PATTERNS_WINDOW_DAYS,
    INSIGHTS_STALE_WINDOW_DAYS,
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
#
# Clarity contract ("the front-page test"): a persisted finding must read like
# a line a human would put in a status report. Measured on the eToro fleet
# BEFORE this contract, 80% of discover findings discussed the clustering
# machinery itself ("Cluster 5 shows std=0.22 ... re-cluster with a finer
# label set") and 67% of their recommendations targeted the memory system
# rather than the work the memories describe — the old "memory analyst"
# persona answered exactly what it was asked. The prompts below flip the
# persona to an operations analyst, make the retrieval mechanics a LENS
# rather than a SUBJECT, and require a structured finding (headline /
# what_happened / why_it_matters / recommended_action). A deterministic
# post-LLM gate (``_gate_findings``) enforces the contract with one
# self-repair retry.

# Shared JSON response contract. Legacy keys (title/description/
# recommendation) are still accepted at sanitize time for models that answer
# old-style, and are mirrored back onto every finding for downstream
# consumers.
_FINDING_JSON_BLOCK = """\
Respond with JSON:
{{
  "findings": [
    {{
      "headline": "one declarative sentence about the work itself (max 80 chars)",
      "what_happened": "2-3 concrete sentences naming the real systems, people, dates and values involved",
      "why_it_matters": "one sentence: the impact or risk if nothing changes",
      "recommended_action": "one imperative sentence starting with a verb, naming who or what should act",
      "confidence": 0.0 to 1.0,
      "related_memory_ids": ["uuid1", "uuid2"]
    }}
  ],
  "summary": "one short paragraph overview"
}}"""

# World modes (discover / patterns): the subject must be the WORK, never the
# records or the analysis machinery.
_RULES_WORLD = """\
RULES — your reader is a busy operator who never sees these records:
- Report on the WORK the records describe (systems, incidents, deals, \
decisions) — never on the records themselves, their grouping, this sample, \
or this analysis. If a group of records reveals nothing about the work \
itself, produce no finding for it.
- Name things by their real names. Never reference numbering from this \
prompt ("record 5", "group 3").
- recommended_action must be something an operator or agent can do in their \
systems or process. Suggestions to write, tag, merge, store or restructure \
records are FORBIDDEN.
- Fewer, sharper findings beat coverage. An empty findings list is a valid \
answer.

BAD finding (never produce): headline "Cluster 5 has high weight variance \
(std=0.22)", action "Re-cluster with a finer label set".
GOOD finding: headline "Health monitoring runs as two disconnected loops", \
what_happened "Heartbeat checks and cleanup escalation fire independently \
across 6 agents; a disk-full event on Jul 28 alerted twice with no shared \
cooldown.", action "Unify triggers and cooldowns in the monitoring workflow \
config."""

# Hygiene modes (contradictions / failures / stale / divergence): the records
# ARE the legitimate subject, but findings must name the disputed fact in
# real-world terms and resolve it concretely.
_RULES_HYGIENE = """\
RULES — your reader is a busy operator deciding what to trust:
- Name the disputed, weak or stale FACT in real-world terms ("gateway \
endpoint: old.example vs new.example"), with the real names, dates and \
values involved.
- Never reference numbering from this prompt ("record 5"); when pointing at \
a specific record, use its (id:...) value in related_memory_ids.
- recommended_action must state the concrete resolution: which version to \
trust and why, what to re-verify, or what to stop relying on — not generic \
bookkeeping.
- Fewer, sharper findings beat coverage. An empty findings list is a valid \
answer."""

_PROMPT_CONTRADICTIONS = (
    """\
You are an operations analyst reviewing the team's work records for \
conflicting claims.

Analyze these {count} records for contradictions. Identify what fact is \
contradicted, which version is likely correct (consider recency and how \
each record was produced), and how to resolve it.

Look for:
- Direct factual contradictions (same thing, different values)
- Corrected facts whose old version may still be trusted somewhere
- Workflow states that cannot both be true (e.g. "sent" vs "blocked")
- Events placed at incompatible times

"""
    + _RULES_HYGIENE
    + """

Records:
{memories}

"""
    + _FINDING_JSON_BLOCK
)

_PROMPT_FAILURES = (
    """\
You are an operations analyst investigating where the team acted on weak or \
unreliable information.

These {count} records carry low reliability yet were used by agents -- \
meaning decisions may rest on shaky ground. Identify what unreliable \
information was acted on, what could go wrong because of it, and what should \
be trusted instead.

Look for:
- Weak information that was used repeatedly
- Recurring kinds of unreliable information and their root causes
- Actions taken on evidence that never confirmed success
- Corrected facts that are still being relied on in their old form

"""
    + _RULES_HYGIENE
    + """

Records:
{memories}

"""
    + _FINDING_JSON_BLOCK
)

_PROMPT_STALE = (
    """\
You are an operations analyst reviewing aging work records for information \
that has likely gone stale.

These {count} records are old or rarely used. Identify which describe facts \
that have probably changed since (versions, endpoints, prices, deadlines, \
owners), what the team might still wrongly assume, and what should be \
re-verified.

Look for:
- Time-sensitive facts recorded once and never revisited
- States ("running", "blocked", "pending") frozen from a past moment
- Old symptoms or incidents that likely no longer describe the system
- Anything that would mislead an operator who trusted it today

"""
    + _RULES_HYGIENE
    + """

Records:
{memories}

"""
    + _FINDING_JSON_BLOCK
)

_PROMPT_DIVERGENCE = (
    """\
You are an operations analyst reviewing where teammates disagree about the \
same things.

These {count} records come from different agents about the same entities. \
Identify where agents hold different beliefs about the same fact, which \
belief is likely correct, and whether the disagreement is real or just \
different contexts.

Look for:
- The same system/entity described with different values or states
- Conflicting conclusions or assessments between agents
- One agent acting on information another agent has already corrected
- Divergence that is actually complementary detail, not conflict

"""
    + _RULES_HYGIENE
    + """

Records:
{memories}

"""
    + _FINDING_JSON_BLOCK
)

_PROMPT_PATTERNS = (
    """\
You are an operations analyst briefing the team on what their recent work \
records show.

Analyze these {count} recent records for what is actually going on in the \
work: emerging themes, repeated struggles, decisions being made, risks \
building up.

Look for:
- The same problem being fought repeatedly instead of fixed
- Shifts in what the team spends its time on
- Decisions and whether their outcomes ever materialized
- Risks or frictions (auth, deploys, data quality) building up quietly

"""
    + _RULES_WORLD
    + """

Records ("[repeats: Nx]" marks a record whose exact operation recurred N \
times in the window — treat the repetition itself as signal about the work):
{memories}

"""
    + _FINDING_JSON_BLOCK
)

_PROMPT_DISCOVER = (
    """\
You are an operations analyst briefing the team on what their work records \
reveal.

These are {count} records grouped by similarity. THE GROUPS ARE A READING \
AID, NOT THE SUBJECT — use them to scan efficiently, then report what the \
records reveal about the work itself.

Look for:
- Recurring operational themes (and whether they represent progress or churn)
- The same real-world problem showing up across several agents
- Work that starts but never visibly completes or gets verified
- Risks or dependencies the team may not have noticed

"""
    + _RULES_WORLD
    + """

Record groups:
{memories}

"""
    + _FINDING_JSON_BLOCK
)


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


async def _query_contradictions(tenant_id, fleet_id, agent_id, scope) -> list[dict]:
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


async def _query_failures(tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch low-weight memories that were recalled (agents acted on weak info).

    ``window_start`` bounds the title-dedup scan — see
    ``INSIGHTS_FAILURES_WINDOW_DAYS``.
    """
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_failures(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=datetime.now(UTC) - timedelta(days=INSIGHTS_FAILURES_WINDOW_DAYS),
    )


async def _query_stale(tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch memories that are likely outdated based on age and recall activity."""
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    # Age thresholds computed on the caller's clock and bound server-side.
    # The window (INSIGHTS_STALE_WINDOW_DAYS) is deliberately wider than the
    # 30-day age threshold rows must exceed to qualify — stale reports the
    # 30-to-window-days "recently became stale" band.
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
        window_start=now - timedelta(days=INSIGHTS_STALE_WINDOW_DAYS),
    )


async def _query_divergence(tenant_id, fleet_id, agent_id, scope) -> list[dict]:
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


async def _query_patterns(tenant_id, fleet_id, agent_id, scope) -> list[dict]:
    """Fetch recent active memories for trend/pattern analysis.

    ``window_start`` (caller's clock, like the stale thresholds) bounds the
    title-dedup scan — see ``INSIGHTS_PATTERNS_WINDOW_DAYS``.
    """
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    return await sc.insights_query_patterns(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=datetime.now(UTC) - timedelta(days=INSIGHTS_PATTERNS_WINDOW_DAYS),
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


async def _query_discover(tenant_id, fleet_id, agent_id, scope) -> _DiscoverResult:
    """Sample memories with embeddings and cluster them in vector space.

    Fix 2 Ph5b: only the row sample routes through storage
    (``sc.insights_discover_sample`` — rows come back as dicts INCLUDING the
    raw ``embedding``); the numpy k-means + cluster-build stay client-side.
    """
    _scope_filters(tenant_id, fleet_id, agent_id, scope)
    sc = get_storage_client()
    # ``rows`` are plain dicts (``_insights_rows_to_dicts(..., include_embedding=True)``)
    # — already the ``_rows_to_dicts`` shape the formatter consumes, with the
    # raw embedding vector for clustering. ``window_start`` (caller's clock,
    # mirroring the stale thresholds) spreads the draw over the trailing
    # window instead of "newest N" — see ``INSIGHTS_DISCOVER_WINDOW_DAYS``.
    rows = await sc.insights_discover_sample(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        scope=scope,
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
        window_start=datetime.now(UTC) - timedelta(days=INSIGHTS_DISCOVER_WINDOW_DAYS),
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
        # Title-dedup annotation (storage collapses repeated exact titles to
        # one exemplar): keep the frequency-and-duration signal the dropped
        # copies carried, at 1/Nth of the token cost. ``first_seen`` is the
        # oldest occurrence among the rows the QUERY considered (windowed for
        # some modes, unbounded legacy for others) — the label is kept
        # neutral so it doesn't overclaim either global history or a window.
        if m.get("dup_count", 1) > 1:
            first_seen = (m.get("first_seen") or "")[:10]
            meta_parts.append(
                f"[repeats: {m['dup_count']}x, first seen: {first_seen}]"
                if first_seen
                else f"[repeats: {m['dup_count']}x]"
            )
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

    Scaffolding vocabulary is deliberately gate-neutral: an earlier revision
    rendered "Cluster {id}" headers and "std=" stats — the exact tokens
    ``_GATE_PROMPT_REF_RE`` / ``_GATE_META_RE`` reject in findings — priming
    the model into violations and burning the repair round on the nightly
    discover run (pre-contract, 80% of discover findings echoed this
    vocabulary when shown it). Numeric cluster ids carry no value to the
    model anyway (findings must reference records by their real ids), so
    headers are anonymous groups, stats are worded, and the record noun
    matches the prompts ("records", never "memories").
    ``test_cluster_scaffolding_is_gate_neutral`` pins this invariant.
    """
    lines = []
    shown_ids: set[str] = set()
    for c in clusters:
        lines.append(f"--- A group of {c['size']} related records ---")
        lines.append(f"  Typical reliability {c['weight_mean']:.2f} (variability {c['weight_std']:.2f})")
        safe_agents = [_sanitize_content(a, max_len=100) for a in c["agents"]]
        lines.append(f"  Agents: {', '.join(safe_agents)} ({c['agent_count']} unique)")
        types = ", ".join(f"{t} x{n}" for t, n in c.get("type_distribution", {}).items())
        lines.append(f"  Record types: {types}")
        lines.append("  Representative records:")
        for r in c.get("representatives", []):
            title = _sanitize_content(r.get("title", "untitled"), max_len=120)
            content = _sanitize_content(r.get("content", ""), max_len=200)
            repeats = f" [repeats: {r['dup_count']}x]" if r.get("dup_count", 1) > 1 else ""
            lines.append(f"    - (id:{r['id']}) [{r.get('memory_type', 'fact')}]{repeats} {title}: {content}")
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
                "headline": "Fake insight for testing",
                "what_happened": "This is a placeholder finding generated by the fake provider.",
                "why_it_matters": "It only exists so tests have a stable shape to assert on.",
                "recommended_action": "Proceed; no action needed (fake provider).",
                "confidence": 0.5,
                "related_memory_ids": [],
            }
        ],
        "summary": "Fake analysis complete.",
    }


# -- Sharpness gate --------------------------------------------------------------
#
# Deterministic post-LLM enforcement of the clarity contract. The prompts
# already forbid machinery-subject findings; the gate catches what slips
# through, with one self-repair retry (the violations are quoted back to the
# LLM). Rejected findings are dropped and counted — a rejected non-finding is
# better than a persisted one.

# World modes must never have the memory/clustering machinery as subject.
_GATE_WORLD_MODES = frozenset({"discover", "patterns"})

# Max violation bullets carried into the repair prompt (the finding count is
# LLM-controlled, so the violation list is unbounded without this).
_REPAIR_MAX_VIOLATIONS = 10

# Machinery-subject markers (world modes: headline or action).
# The memory-noun alternative must not false-positive on legitimate
# operational findings about RAM/heap ("Memory usage spiked on the batch
# worker"): fixed-width lookbehinds exclude hardware qualifiers and a
# lookahead excludes resource-usage nouns, so only STORED-memory talk
# ("multiple memories show...") counts as machinery-subject.
# Two alternatives carry false-positive guards because the machinery words
# double as legitimate infrastructure vocabulary:
# - "memory": RAM/heap findings ("Memory usage spiked") are real operations
#   subject matter — hardware qualifiers and resource nouns are excluded, so
#   only STORED-memory talk ("multiple memories show...") counts.
# - "cluster": compute clusters (Spark/Databricks/Kafka...) are real systems
#   — named-technology qualifiers and capacity nouns are excluded, so only
#   similarity-grouping talk ("Cluster 5", "re-cluster...") counts.
# "cluster" detection is POSITIVE-SIGNAL, not an allowlist: an earlier
# revision excluded a fixed set of technologies via lookbehinds
# (spark/kafka/databricks/...), but legitimate infra phrasings are unbounded
# ("application cluster", "staging cluster", "EKS cluster"...) and every miss
# silently drops a valid finding. Instead, bare "cluster" only counts as
# machinery when it co-occurs with clustering-analysis vocabulary; the
# numbered ("Cluster 3" — _GATE_PROMPT_REF_RE), "re-cluster", "std=" and
# "weight variance" cases are caught by their own checks. Trade-off: an
# un-numbered, un-signalled machinery sentence ("this cluster is coherent")
# relies on the prompt rules + repair loop rather than this regex.
_GATE_META_RE = re.compile(
    r"\bembeddings?\b|\bknowledge (base|graph|coverage|topolog)"
    r"|\btaxonom|\bdedup|\bstd\s*=|weight variance|\bre-?cluster|\bthis (sample|analysis)\b"
    r"|\b(similarity|embedding|semantic|record|memory) clusters?\b"
    r"|\bclusters? of (records?|memor(y|ies)|episodes?|findings?|similar)\b"
    r"|\b(singleton|bridge|missing|high.variance) clusters?\b"
    r"|\bclusters? (formation|boundar|separation|overlap|topolog)"
    # Hardware-memory exclusions: qualifier before the word (incl. capacity
    # qualifiers: peak/available/free/used/total) OR resource noun/verb after
    # it (incl. capacity movements: dropped/exceeded/critical/high). Residual
    # known gap: a bare unqualified "Memory hit 90%" still flags — covered by
    # the repair loop rather than growing this list unboundedly.
    r"|(?<!out of )(?<!gpu )(?<!ram )(?<!heap )(?<!swap )(?<!system )(?<!shared )(?<!virtual )(?<!physical )"
    r"(?<!peak )(?<!available )(?<!free )(?<!used )(?<!total )"
    r"\bmemor(y|ies)\b(?!\s+(usage|leak|pressure|consumption|footprint|limit|spike|error|exhaust"
    r"|utili[sz]ation|alloc|dropped|exceed|critical|high))",
    re.IGNORECASE,
)

# Bookkeeping actions (world modes: the action must act on the world).
# Two precision constraints, each pinned by tests:
# - The verb may sit after a short hedge/subject prefix ("Analysts should
#   record...", "Consider merging..."), so allow up to three FILLER words
#   before it — a whitelist, not arbitrary words, lest real actions whose
#   later words overlap these verbs get caught ("Fix the config writing
#   logic" must pass).
# - The verb alone is NOT enough: "Store the API credentials in the secrets
#   manager", "Tag the PagerDuty incident as P1", "Merge the duplicate CRM
#   vendor entries" are real operator actions. The verb must take a
#   record-machinery OBJECT within a few words ("Record a postmortem
#   memory...", "Consolidate these records..."). Exceptions: "re-cluster"
#   (inherently machinery) and the "add a memory/metadata/tag/cluster" form,
#   which already carries its object.
_GATE_BOOKKEEPING_RE = re.compile(
    r"^\s*((please|consider|agents?|analysts?|operators?|teams?|we|you|should|must|could|can|then|also)\s+){0,3}"
    r"(?:(?:record(ing)?|tag(ging)?|stor(e|ing)|captur(e|ing)|annotat(e|ing)|merg(e|ing)|consolidat(e|ing)"
    r"|writ(e|ing)|link(ing)?|supersed(e|ing)|deprecat(e|ing))\b"
    r"(?:\s+[\w-]+){0,3}?\s+(memor(y|ies)|records?|findings?|insights?|clusters?|groups?)\b"
    r"|re-?cluster(ing)?\b"
    # "memor" needs its suffixes spelled out (as in _GATE_META_RE): the
    # group-closing \b can never fire after the bare stem — "memory"/"memories"
    # continue with word characters — so "Add a memory..." went uncaught.
    r"|add (a |an )?(memor(y|ies)?|metadata|tag|cluster)\b)",
    re.IGNORECASE,
)

# Prompt-local numbering (all modes): meaningless once persisted.
# Deliberately narrow ("memory 5" / "cluster 3" only): "record"/"group"
# false-positive on legitimate operational ids ("task group 4", "record #123"),
# and the repair loop plus the prompt rules cover the residual phrasings.
_GATE_PROMPT_REF_RE = re.compile(r"\b(memor(y|ies)|cluster(s|ing|ed)?)\s+#?\d", re.IGNORECASE)


def _gate_findings(findings: list[dict], focus: str) -> tuple[list[dict], list[str]]:
    """Split sanitized findings into (passed, violation_messages).

    Hygiene modes (contradictions/failures/stale/divergence) legitimately have
    the records as subject — only the prompt-local-numbering check applies.
    World modes additionally reject machinery subjects and bookkeeping
    actions.
    """
    passed: list[dict] = []
    violations: list[str] = []
    for f in findings:
        headline = f.get("headline", "")
        action = f.get("recommended_action", "")
        text_fields = " | ".join(
            str(f.get(k, "")) for k in ("headline", "what_happened", "why_it_matters", "recommended_action")
        )
        problems = []
        if _GATE_PROMPT_REF_RE.search(text_fields):
            problems.append(
                "references prompt-local numbering (e.g. 'record 5') — name things by their real names"
            )
        if focus in _GATE_WORLD_MODES:
            # All four fields, not just headline/action: machinery talk that
            # hides in what_happened/why_it_matters ("the embedding separated
            # them by workflow type") is the same defect.
            if _GATE_META_RE.search(text_fields):
                problems.append(
                    "subject is the records/clustering machinery — report on the work the records describe"
                )
            if _GATE_BOOKKEEPING_RE.search(action):
                problems.append(
                    "recommended_action is record bookkeeping — it must act on systems or process"
                )
        if problems:
            violations.append(f'"{headline[:80]}": ' + "; ".join(problems))
        else:
            passed.append(f)
    return passed, violations


_REPAIR_SUFFIX = """

YOUR PREVIOUS ATTEMPT violated the rules for these findings:
{violations}

Return findings JSON containing ONLY corrected versions of the violating
findings listed above — fix each so it complies with the RULES, or omit it
entirely if no compliant version exists. Do NOT repeat findings that already
complied, and do NOT introduce new findings. Same JSON shape, plus one extra
field on each corrected finding: "repairs" — the ORIGINAL violating headline
copied verbatim from the list above (this is a correlation key; corrected
findings without a matching "repairs" value are discarded)."""


# -- Persist -------------------------------------------------------------------


async def _persist_findings(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
    focus: str,
    scope: str,
    findings: list[dict],
    method: dict | None = None,
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
        headline = str(finding.get("headline") or finding.get("title") or "Untitled insight")[:80]
        titles.append(headline)
        what_happened = str(finding.get("what_happened") or finding.get("description") or "")[:1000]
        why_it_matters = str(finding.get("why_it_matters") or "")[:300]
        action = str(finding.get("recommended_action") or finding.get("recommendation") or "")[:500]
        confidence = max(0.0, min(1.0, float(finding.get("confidence", 0.5))))
        related_ids = finding.get("related_memory_ids", [])

        # Clarity contract renderer: the headline leads (enrichment derives
        # the memory ``title`` from it — ``BulkMemoryItem`` has no title
        # field), followed by short labeled lines instead of the old run-on
        # "[Insight/{type}] {title}: {description} Recommendation: ..."
        # paragraph. Method/provenance lives in metadata, NOT in the text.
        lines = [headline]
        if what_happened:
            lines.append(f"What happened: {what_happened}")
        if why_it_matters:
            lines.append(f"Why it matters: {why_it_matters}")
        if action:
            lines.append(f"Action: {action}")
        content = "\n".join(lines)

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
                    "headline": headline,
                    "recommendation": action,
                    "confidence": confidence,
                    # Method transparency: how this finding was produced —
                    # kept out of the finding text, auditable here.
                    "method": method or {"focus": focus, "scope": scope},
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
        response = await create_memories_bulk(bulk_data, bulk_attempt_id=bulk_attempt_id)
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


def _build_method(focus: str, scope: str, is_clustered: bool, synth: dict) -> dict:
    """Run-level provenance stamped into each finding's ``metadata.method``.

    Method transparency lives HERE (and in logs), never in the finding text —
    the clarity contract keeps the insight sharp and the method auditable."""
    return {
        "focus": focus,
        "scope": scope,
        "memories_analyzed": synth.get("memories_analyzed", 0),
        "clustered": is_clustered,
        "gate_rejected": synth.get("gate_rejected", 0),
    }


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
    sanitized, summary = _sanitize_findings(analysis, shown_ids, focus=focus, scope=scope)

    # Sharpness gate + one self-repair retry: quote the violations back to
    # the LLM once; findings that still violate after the retry are dropped.
    passed, violations = _gate_findings(sanitized, focus)
    gate_rejected = 0
    if violations:
        # Identity-based rescue tracking: violators are the sanitized
        # findings that did NOT pass (same dict objects, so identity
        # comparison is exact), and each violation message in ``violations``
        # was appended in the same iteration order — zip-aligned below.
        passed_ids = {id(f) for f in passed}
        violators = [f for f in sanitized if id(f) not in passed_ids]
        # FIFO queue per casefolded headline, NOT a flat dict: near-duplicate
        # LLM headlines within one batch are likely, and a dict comprehension
        # would silently collapse same-headline violators onto the last one —
        # breaking echo correlation and the rescue accounting for exactly
        # that case. With queues, each rescue echoing a shared headline
        # consumes the next unfixed violator carrying it.
        violators_by_headline: dict[str, list[dict]] = {}
        for v in violators:
            violators_by_headline.setdefault(v.get("headline", "").casefold(), []).append(v)
        violation_msg_by_id = {id(v): msg for v, msg in zip(violators, violations, strict=True)}

        # Repair-round trigger rate is the health metric for the clarity
        # contract: the nightly lifecycle job runs discover for every tenant,
        # so a high rate here means the prompts are priming violations (and
        # every trigger is a second LLM call). One greppable line per
        # invocation — watch its frequency after deploy.
        logger.info(
            "insights: gate flagged %d of %d findings; invoking repair round (focus=%s, scope=%s)",
            len(violations),
            len(sanitized),
            focus,
            scope,
        )
        # The LLM controls the finding count, so the violation list is
        # unbounded — cap what the repair prompt carries. Violators beyond
        # the cap simply can't be rescued (their headlines aren't in the
        # prompt) and the identity-based accounting counts them rejected.
        shown_violations = violations[:_REPAIR_MAX_VIOLATIONS]
        if len(violations) > len(shown_violations):
            logger.info(
                "insights: repair prompt capped to %d of %d violations (focus=%s, scope=%s)",
                len(shown_violations),
                len(violations),
                focus,
                scope,
            )
        repair_prompt = prompt + _REPAIR_SUFFIX.format(
            violations="\n".join(f"- {v}" for v in shown_violations)
        )
        try:
            repaired = await _run_llm_analysis(repair_prompt, config)
        except Exception:
            logger.warning(
                "insights: repair pass failed; keeping first-pass compliant findings", exc_info=True
            )
        else:
            re_sanitized, re_summary = _sanitize_findings(repaired, shown_ids, focus=focus, scope=scope)
            re_passed, _still_violating = _gate_findings(re_sanitized, focus)
            # MERGE, never replace: findings that already passed the
            # first-pass gate are kept unconditionally — an incomplete or
            # malformed repair response can only fail to rescue violators,
            # never lose compliant work. A repair-pass finding counts as a
            # rescue ONLY if it ties back to a flagged violator: via its
            # "repairs" echo key (the repair prompt requires the original
            # violating headline verbatim) or, fallback, by keeping the
            # violator's own headline while fixing the body. Anything else
            # — inventions, echoes of kept findings — is dropped, so the
            # repair cannot smuggle in findings the first pass never
            # produced, and an invention can't mask an unrescued violator
            # in the accounting.
            kept_headlines = {f.get("headline", "").casefold() for f in passed}
            rescued: list[dict] = []
            fixed_violator_ids: set[int] = set()
            for f in re_passed:
                echo = str(f.pop("repairs", "") or "").casefold()
                own = f.get("headline", "").casefold()
                queue = violators_by_headline.get(echo) or violators_by_headline.get(own) or []
                violator = next((v for v in queue if id(v) not in fixed_violator_ids), None)
                if violator is None or own in kept_headlines:
                    continue
                fixed_violator_ids.add(id(violator))
                rescued.append(f)
                # Rescued findings join the dedup set so two rescues can't
                # merge under one identical new headline.
                kept_headlines.add(own)
            passed = passed + rescued
            # gate_rejected = original violators minus confirmed-fixed ones;
            # the surviving messages are exactly the unfixed violators'.
            violations = [violation_msg_by_id[id(v)] for v in violators if id(v) not in fixed_violator_ids]
            summary = re_summary or summary
        gate_rejected = len(violations)
        if gate_rejected:
            logger.info(
                "insights: gate dropped %d findings after repair (focus=%s, scope=%s): %s",
                gate_rejected,
                focus,
                scope,
                " | ".join(violations)[:500],
            )

    return {
        # A first-pass model that hallucinates the repair-only "repairs" key
        # must not leak it to consumers/persist (rescued findings had theirs
        # popped during correlation).
        "findings": [{k: v for k, v in f.items() if k != "repairs"} for f in passed],
        "summary": summary,
        "memories_analyzed": count,
        "gate_rejected": gate_rejected,
    }


def _sanitize_findings(
    analysis: dict, shown_ids: set[str], *, focus: str, scope: str
) -> tuple[list[dict], str]:
    """Normalize an LLM response into finding dicts.

    New schema (headline / what_happened / why_it_matters /
    recommended_action) with fallbacks from the legacy keys (title /
    description / recommendation), so a model that answers old-style still
    works. Every finding also carries the legacy keys MIRRORED from the new
    fields — downstream consumers (digest agents, dashboards) that read
    ``title``/``description``/``recommendation`` keep working unchanged.
    """
    findings = analysis.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    sanitized: list[dict] = []
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
        # Same length caps the persist path applies — the gate and the
        # repair prompt must operate on the exact bounded text that
        # ultimately gets persisted, not on a longer variant.
        headline = str(f.get("headline") or f.get("title") or "Untitled")[:80]
        what_happened = str(f.get("what_happened") or f.get("description") or "")[:1000]
        why_it_matters = str(f.get("why_it_matters") or "")[:300]
        action = str(f.get("recommended_action") or f.get("recommendation") or "")[:500]
        item = {
            "type": str(f.get("type", focus))[:50],
            "headline": headline,
            "what_happened": what_happened,
            "why_it_matters": why_it_matters,
            "recommended_action": action,
            "confidence": max(0.0, min(1.0, _to_float(f.get("confidence", 0.5)))),
            "related_memory_ids": kept_related,
            # Legacy mirrors — see docstring.
            "title": headline,
            "description": " ".join(p for p in (what_happened, why_it_matters) if p),
            "recommendation": action,
        }
        # Repair-pass correlation key: the repair prompt asks each corrected
        # finding to echo the ORIGINAL violating headline verbatim so the
        # merge can tie the rescue back to a flagged violator. Carried only
        # when present; the merge block consumes (pops) it.
        if f.get("repairs"):
            item["repairs"] = str(f.get("repairs"))[:80]
        sanitized.append(item)
    if total_dropped > 0:
        logger.info(
            "insights: dropped %d hallucinated related_memory_ids across %d findings (focus=%s, scope=%s)",
            total_dropped,
            findings_with_drops,
            focus,
            scope,
        )
    return sanitized, analysis.get("summary", "")


async def generate_insights(
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
    memories_or_clusters = await query_fn(tenant_id, fleet_id, agent_id, scope)

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
            "gate_rejected": 0,
            "insights_ms": int((time.perf_counter() - t0) * 1000),
        }

    # 2. Resolve tenant config for LLM provider
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)

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
    method = _build_method(focus, scope, is_clustered, synth)
    insight_ids = await _persist_findings(
        tenant_id, agent_id, fleet_id, focus, scope, findings, method=method
    )

    return {
        "focus": focus,
        "scope": scope,
        "memories_analyzed": synth["memories_analyzed"],
        "findings": [{**f, "insight_memory_id": mid} for f, mid in zip(findings, insight_ids)],
        "summary": synth["summary"],
        "insight_memory_ids": [mid for mid in insight_ids if mid],
        "gate_rejected": synth.get("gate_rejected", 0),
        "insights_ms": int((time.perf_counter() - t0) * 1000),
    }
