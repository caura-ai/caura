"""Procedure ranking + reliability math (Procedural Memory PM-02).

The runtime half of the procedural-memory loop. Ports Brain's
``ProcedureRanker`` (procedural_memory_mcp/src/retrieval/ranker.py) onto
MemClaw infrastructure: candidates and reliability come from
core-storage via :func:`core_api.clients.storage_client.get_storage_client`,
and the semantic component reuses MemClaw's embedding provider via
:func:`common.embedding.get_query_embedding` — Brain's own DB / embedder
are NOT imported.

A procedure's rank is a transparent blend of three signals:

    score = W_SEMANTIC   * cosine(query_embedding, procedure_embedding)
          + W_CONTEXT    * jaccard(query_context, procedure_context)
          + W_RELIABILITY * reliability_score

Quarantined procedures never reach the ranker — core-storage filters them
out of the candidate list. ``record`` (PM-03) is the write side that moves
``reliability_score`` and flips quarantine; this module is read/rank only.
"""

from __future__ import annotations

import math
from typing import Any

from common.embedding import get_query_embedding
from core_api.clients.storage_client import get_storage_client

# Blend weights. Reliability and context are weighted equally with the
# semantic signal so a high-reliability, well-matched procedure outranks a
# semantically-close but unproven one — the whole point of the loop.
W_SEMANTIC: float = 0.4
W_CONTEXT: float = 0.3
W_RELIABILITY: float = 0.3

# How many candidates to pull from storage before in-process ranking.
_CANDIDATE_LIMIT = 200


def _flatten(d: dict[str, Any]) -> list[Any]:
    """Flatten a nested context-features dict to a list of scalar leaves."""
    out: list[Any] = []
    for v in d.values():
        if isinstance(v, dict):
            out.extend(_flatten(v))
        elif isinstance(v, list):
            out.extend(v)
        else:
            out.append(v)
    return out


def _context_overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Jaccard overlap of two context-feature dicts (ported from Brain).

    Both sides are flattened to their scalar leaves, stringified, and
    compared as sets. Empty on either side → 0.0.
    """
    aset = set(map(str, _flatten(a)))
    bset = set(map(str, _flatten(b)))
    if not aset or not bset:
        return 0.0
    union = len(aset | bset)
    return len(aset & bset) / union if union else 0.0


def _construct_query(task: str, context_features: dict[str, Any]) -> str:
    """Build a natural-language query from task + context (ported from Brain)."""
    parts: list[str] = []
    if task:
        parts.append(f"Goal: {task}")
    if context_features:
        if "framework" in context_features:
            parts.append(f"using {context_features['framework']} framework")
        if "test_framework" in context_features:
            parts.append(f"testing with {context_features['test_framework']}")
        if "library" in context_features:
            parts.append(f"using library {context_features['library']}")
        skip = {"framework", "test_framework", "library", "os", "arch"}
        other = [
            f"{k}: {v}"
            for k, v in context_features.items()
            if k not in skip and isinstance(v, (str, int, float))
        ]
        if other:
            parts.append(f"Context: {', '.join(other)}")
    return " | ".join(parts)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two vectors; 0.0 if either is missing/empty."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def rank_procedures(
    tenant_id: str,
    context_features: dict[str, Any] | None,
    *,
    task: str | None = None,
    fleet_id: str | None = None,
    limit: int = 5,
    tenant_config: object | None = None,
) -> list[dict[str, Any]]:
    """Return the top-``limit`` procedures for a context, ranked.

    Each result is ``{"procedure": <dict>, "score": float, "breakdown":
    {"semantic": float, "context_overlap": float, "reliability": float}}``.
    Empty list when the tenant has no (non-quarantined) procedures.
    """
    context_features = context_features or {}
    sc = get_storage_client()
    candidates = await sc.list_procedures(
        tenant_id,
        fleet_id=fleet_id,
        include_quarantined=False,
        limit=_CANDIDATE_LIMIT,
    )
    if not candidates:
        return []

    query = _construct_query(task or "", context_features)
    q_emb = await get_query_embedding(query, tenant_config) if query.strip() else None

    scored: list[dict[str, Any]] = []
    for proc in candidates:
        ctx = proc.get("context_features") or {}
        overlap = _context_overlap(context_features, ctx)
        semantic = _cosine(q_emb, proc.get("embedding"))
        stats = proc.get("stats") or {}
        reliability = stats.get("reliability_score", 0.5)
        score = W_SEMANTIC * semantic + W_CONTEXT * overlap + W_RELIABILITY * reliability
        scored.append(
            {
                "procedure": proc,
                "score": score,
                "breakdown": {
                    "semantic": semantic,
                    "context_overlap": overlap,
                    "reliability": reliability,
                },
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


def compute_reliability(success_count: int, failure_count: int) -> float:
    """Laplace-smoothed success rate used as ``reliability_score``.

    ``(success + 1) / (success + failure + 2)`` — starts at 0.5 with no
    evidence (matching the column default), rises with wins, falls with
    losses, and never hits 0 or 1 so a single later outcome can still move
    it. This is the value ``record`` (PM-03) writes back.
    """
    return (success_count + 1) / (success_count + failure_count + 2)


async def bump_skill_telemetry(
    skill_doc_id: str,
    tenant_id: str,
    outcome_type: str,
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    """Increment a Forge-minted skill's telemetry block on a procedure outcome.

    This is the concrete implementation of the Skill Factory's deferred
    "Phase-4 outcome loop" (skill-factory-implementation-plan.md §3 telemetry,
    §15 Phase 4): every ``memclaw_procedure_record`` against a procedure that
    links back to a skill (``skill_doc_id``) moves the parent skill's
    ``fires_total`` / ``fires_success`` / ``fires_failure`` / ``last_fired_at``.

    Read-modify-write of the whole ``documents.data`` jsonb so no sibling
    field is lost. Goes through the storage documents surface directly — it
    only touches the ``telemetry`` sub-object, never ``status``, so the
    skill-lifecycle RBAC on status transitions is not engaged. Returns the
    updated telemetry dict, or ``None`` if the skill doc no longer exists
    (tolerated — a deleted skill simply has nowhere to count).
    """
    sc = get_storage_client()
    doc = await sc.get_document(tenant_id=tenant_id, collection="skills", doc_id=skill_doc_id)
    if doc is None:
        return None
    data = dict(doc.get("data") or {})
    tel = dict(data.get("telemetry") or {})
    tel["fires_total"] = int(tel.get("fires_total", 0)) + 1
    if outcome_type == "success":
        tel["fires_success"] = int(tel.get("fires_success", 0)) + 1
    else:
        tel["fires_failure"] = int(tel.get("fires_failure", 0)) + 1
    tel["last_fired_at"] = now_iso
    data["telemetry"] = tel
    doc["data"] = data
    await sc.upsert_document(doc)
    return tel
