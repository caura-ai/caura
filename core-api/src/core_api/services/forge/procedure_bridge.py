"""Forge → procedures bridge (Procedural Memory PM-04).

When Forge mints a skill candidate from a session-trace cluster, this
module derives a *structured procedure* from the same cluster so the
mined knowledge is runtime-**suggestable** (via ``memclaw_procedure_suggest``),
not only installable as a SKILL.md. The procedure links back to the skill
document via ``skill_doc_id``.

The emission is additive and failure-isolated: ``run_forge_distill`` calls
the emitter only after ``candidate_writer`` has already persisted the skill,
inside its own try/except, so a procedure-emission failure can never block
or roll back skill minting.

``tools_sequence`` v1 note: session traces do not yet carry an explicit
ordered tool-call list — the harness records *memories*, not tool calls,
into a trace. So v1 uses the representative trace's ordered ``memory_ids``
as the action-sequence proxy. When the harness begins emitting real
tool-call ids into ``signals_summary`` (a future signal extractor), swap
:func:`_extract_tools_sequence` to read them; nothing else changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core_api.services.procedure_service import compute_reliability

# (procedure_dict) → None. Mirrors CandidateWriter's injection shape.
ProcedureEmitter = Callable[[dict[str, Any]], Awaitable[None]]


def _representative_trace(cluster_traces: list[Any]) -> Any:
    """The trace whose member set best represents the cluster's action flow.

    Longest ``memory_ids`` wins — it carries the most of the cluster's
    activity. Ties broken by most-recent ``ended_at`` so the freshest
    flow is preferred.
    """
    return max(
        cluster_traces,
        key=lambda t: (len(t.memory_ids or []), t.ended_at),
    )


def _extract_tools_sequence(rep: Any) -> list[str]:
    """Ordered action sequence for the procedure (v1: trace memory_ids)."""
    return list(rep.memory_ids or [])


def build_procedure_from_cluster(
    cluster_traces: list[Any],
    candidate_doc: dict[str, Any],
    *,
    tenant_id: str,
    fleet_id: str | None,
) -> dict[str, Any]:
    """Build a procedures-row payload from a Forge cluster + its skill doc.

    Seeds reliability from the cluster's own outcome labels so a procedure
    mined from proven-successful traces starts above 0.5 and one mined from
    failures starts below — the ranker then weights it immediately, before
    any live ``memclaw_procedure_record`` call.
    """
    data = candidate_doc.get("data", {})
    rep = _representative_trace(cluster_traces)

    entity_ids = sorted({e for t in cluster_traces for e in (t.entity_ids or [])})
    context_features: dict[str, Any] = {"entities": entity_ids[:10]}
    goal = data.get("goal") or rep.goal_phrase
    if goal:
        context_features["goal"] = goal

    successes = sum(1 for t in cluster_traces if t.outcome_label == "success")
    failures = sum(1 for t in cluster_traces if t.outcome_label == "failure")
    reliability = compute_reliability(successes, failures)

    return {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": "forge",
        "name": data.get("name") or data.get("slug") or "forge-procedure",
        "pattern_signature": data.get("cluster_fingerprint")
        or data.get("slug")
        or candidate_doc.get("doc_id"),
        "tools_sequence": _extract_tools_sequence(rep),
        "context_features": context_features,
        "reasoning_guide": data.get("summary"),
        "skill_doc_id": candidate_doc.get("doc_id"),
        # Mirror the skill's lifecycle entry state — Forge candidates land
        # as 'candidate'; PM-05 / the lifecycle worker can promote later.
        "status": data.get("status", "candidate"),
        "stats": {
            "success_count": successes,
            "failure_count": failures,
            "reliability_score": reliability,
        },
    }
