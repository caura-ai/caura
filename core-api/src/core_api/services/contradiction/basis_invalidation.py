"""Path D — basis invalidation, SHADOW MODE (A58).

The Type-II gap: a new memory can destroy the practical basis of an OLD
memory about a *different* attribute of the same subject ("tore ACL, no
weight-bearing" retires "commutes by bicycle") without contradicting it.
Path A's question — "do these two make incompatible claims?" — is the wrong
question for that failure mode, and cannot be tuned into the right one
without a false-positive explosion (see
``Downloads/cupmem_stale_contradiction_analysis.md`` §4.3).

This module ports the portable core of CUP-Mem's write side (STALE paper,
arXiv:2605.06527 — steps 6-8 of its pipeline) onto caura's existing
``(subject_entity_id, predicate)`` slot model:

    1. bridge (1 LLM call)  — which OTHER predicates of this subject may
       have had their practical basis broken; menu-gated, ≤3.
    2. scoped fetch (0 LLM) — active rows for (subject, bridged predicate)
       via the existing rdf-conflicts endpoint; when the predicate route is
       empty (predicates are sparsely populated today — A63's
       canonicalization is still in flight), falls back to the caller's
       entity-overlap candidates (already fetched by Path C, zero extra
       storage calls). Verdict lines carry ``route=rdf|overlap`` so the
       spike can measure both.
    3. judge (≤2 LLM calls) — INDIRECT_INVALIDATE | WEAK_CHALLENGE |
       SET_UNKNOWN_CURRENT | NO_OP per surviving candidate.

**SHADOW MODE — this module never writes.** Verdicts are logged
(``path_d_shadow``) so the Type-II base rate and precision can be measured
on real corpora before any status write ships (that build is A59, including
the ``unsafe`` status / migration 037). Flag: ``settings.basis_invalidation_shadow``
(default off), mirroring ``contradiction_write_conflict_record``.

Failure isolation: the single entry point swallows everything — Path D can
never affect Path A's outcome or the write path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from common.constants import SINGLE_VALUE_PREDICATES
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)

# Across ALL bridged predicates, at most this many candidates reach the judge
# (CUP-Mem ``invalidation_merge_max_keep``). Keeps worst-case LLM cost at
# bridge + 2 judges per write.
MAX_JUDGED = 2
MAX_BRIDGE_PREDICATES = 3
MIN_BRIDGE_CONFIDENCE = 0.5

# Ported guardrails: the "practical basis" vocabulary and the
# topic-overlap-is-not-enough clause are what keep precision up
# (BUCKET_BRIDGE_PROPOSER_PROMPT). The menu is a hard gate — an off-menu
# predicate is dropped, the analog of CUP-Mem's ``is_valid_bucket_track``.
BASIS_BRIDGE_PROMPT = """\
A memory store tracks facts about a subject as (predicate, value) pairs. A new \
memory about this subject just arrived.

New memory: "{new_content}"
Its predicate (if any): {new_predicate}

MENU of other single-value predicates the store can track:
{menu}

Propose AT MOST {max_n} predicates FROM THE MENU whose currently stored value \
may no longer be safe to rely on as the subject's CURRENT default, because the \
new memory broke its practical basis. Practical basis includes: access, \
availability, continuity, recoverability, feasibility, responsibility, \
arrangement, recurring coordination, institutional or status dependency.

Rules:
- Name the SPECIFIC broken basis for each proposal. Shared life domain, topic \
overlap, or general relatedness is NOT enough.
- Only predicates from the menu. If nothing qualifies, return an empty list — \
an empty list is the common, correct answer.

Return JSON only: {{"affected": [{{"predicate": "<from menu>", \
"broken_basis": "<one sentence>", "confidence": <0..1>}}]}}"""

# Ported INVALIDATION_JUDGE_PROMPT semantics. The enum is kept verbatim for
# comparability with the paper; in caura terms every non-NO_OP verdict would
# map to the future ``unsafe`` status (A59).
BASIS_JUDGE_PROMPT = """\
New evidence about a subject: "{new_content}"

Old stored memory (predicate "{predicate}" of the same subject): "{old_content}"

Question: does the new evidence BREAK THE PRACTICAL BASIS that made the old \
memory safe to use as the subject's current default? This is NOT a \
contradiction check — both statements may be simultaneously true, and the old \
one can still be unsafe to act on.

Guardrails:
- Same-subject or topic proximity alone is not enough.
- Do not invalidate the old memory merely because it is older or related.
- Decide INDIRECT_INVALIDATE only when you can name the broken basis.

Decisions:
- INDIRECT_INVALIDATE: the old default's practical basis is broken.
- WEAK_CHALLENGE: partially undermined; old value should be treated cautiously.
- SET_UNKNOWN_CURRENT: the old default is unsafe AND no replacement is known.
- NO_OP: the old memory remains a safe current default.

Return JSON only: {{"decision": "<one of the four>", "reason": "<one \
sentence naming the basis>", "confidence": <0..1>}}"""

_DECISIONS = {"INDIRECT_INVALIDATE", "WEAK_CHALLENGE", "SET_UNKNOWN_CURRENT", "NO_OP"}


def _parse_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return {}


async def _bridge(new_content: str, new_predicate: str | None, menu: list[str], tenant_config) -> list[dict]:
    prompt = BASIS_BRIDGE_PROMPT.format(
        new_content=new_content[:500],
        new_predicate=new_predicate or "(none)",
        menu=", ".join(menu),
        max_n=MAX_BRIDGE_PREDICATES,
    )
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    async def _do(llm) -> list[dict]:
        raw = _parse_json(await llm.complete_json(prompt))
        out = []
        allowed = set(menu)
        for p in raw.get("affected", [])[:MAX_BRIDGE_PREDICATES]:
            pred = str(p.get("predicate", "")).strip()
            conf = float(p.get("confidence") or 0.0)
            # hard menu gate + confidence floor — the precision guardrails
            if pred in allowed and conf >= MIN_BRIDGE_CONFIDENCE:
                out.append(
                    {"predicate": pred, "broken_basis": str(p.get("broken_basis", "")), "confidence": conf}
                )
        return out

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do,
        fake_fn=lambda: [],
        tenant_config=tenant_config,
        service_label="basis_bridge",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


async def _judge(new_content: str, candidate: dict, tenant_config) -> dict:
    prompt = BASIS_JUDGE_PROMPT.format(
        new_content=new_content[:500],
        predicate=candidate.get("predicate", ""),
        old_content=(candidate.get("content") or "")[:500],
    )
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    async def _do(llm) -> dict:
        raw = _parse_json(await llm.complete_json(prompt))
        decision = str(raw.get("decision", "NO_OP")).strip().upper()
        if decision not in _DECISIONS:
            decision = "NO_OP"
        return {
            "decision": decision,
            "reason": str(raw.get("reason", "")),
            "confidence": float(raw.get("confidence") or 0.0),
        }

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do,
        fake_fn=lambda: {"decision": "NO_OP", "reason": "fake provider", "confidence": 0.0},
        tenant_config=tenant_config,
        service_label="basis_judge",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


async def run_basis_shadow(
    new_memory: dict,
    tenant_id: str,
    fleet_id: str | None,
    tenant_config=None,
    overlap_candidates: list[dict] | None = None,
) -> dict:
    """Run Path D end-to-end in shadow mode. Returns a summary dict (for
    tests and the shadow-spike harness); the only side effect is logging."""
    t0 = time.monotonic()
    memory_id = str(new_memory.get("id", ""))
    summary: dict[str, Any] = {
        "memory_id": memory_id,
        "fired": False,
        "skipped_reason": None,
        "bridged": [],
        "verdicts": [],
    }
    try:
        subject = new_memory.get("subject_entity_id")
        if not subject:
            # No subject → no dependency graph → no point paying for the call.
            summary["skipped_reason"] = "no_subject_entity_id"
            return summary
        new_content = new_memory.get("content") or ""
        if not new_content.strip():
            summary["skipped_reason"] = "empty_content"
            return summary
        new_predicate = new_memory.get("predicate") or None
        menu = sorted(SINGLE_VALUE_PREDICATES - ({new_predicate} if new_predicate else set()))

        bridged = await _bridge(new_content, new_predicate, menu, tenant_config)
        summary["fired"] = True
        summary["bridged"] = bridged
        if not bridged:
            logger.info(
                "path_d_shadow completed memory=%s subject=%s bridged=0 verdicts=0 ms=%d",
                memory_id,
                subject,
                int((time.monotonic() - t0) * 1000),
            )
            return summary

        sc = get_storage_client()
        candidates: list[dict] = []
        route = "rdf"
        for b in sorted(bridged, key=lambda x: -x["confidence"]):
            rows = await sc.find_rdf_conflicts(
                tenant_id=tenant_id,
                subject_entity_id=str(subject),
                predicate=b["predicate"],
                exclude_id=memory_id or None,
                fleet_id=fleet_id,
            )
            for r in rows:
                if r.get("status") in ("outdated", "deleted"):
                    continue  # already retired — nothing to invalidate
                candidates.append({**r, "predicate": b["predicate"], "broken_basis": b["broken_basis"]})
            if len(candidates) >= MAX_JUDGED:
                break
        if not candidates and overlap_candidates:
            # Fallback: predicates are sparsely populated on real rows today,
            # so the rdf route often has nothing to key on. Path C's
            # entity-overlap candidates are the same-subject pool, already
            # fetched — judge the freshest ones and mark the route.
            route = "overlap"
            top_basis = bridged[0]["broken_basis"]
            for r in overlap_candidates:
                if str(r.get("id", "")) == memory_id:
                    continue
                if r.get("status") in ("outdated", "deleted"):
                    continue
                candidates.append(
                    {**r, "predicate": r.get("predicate") or "(none)", "broken_basis": top_basis}
                )
                if len(candidates) >= MAX_JUDGED:
                    break
        summary["route"] = route
        candidates = candidates[:MAX_JUDGED]

        for c in candidates:
            verdict = await _judge(new_content, c, tenant_config)
            entry = {
                "target_memory_id": str(c.get("id", "")),
                "predicate": c.get("predicate"),
                "broken_basis": c.get("broken_basis"),
                **verdict,
            }
            summary["verdicts"].append(entry)
            # One greppable line per verdict — this IS the shadow deliverable.
            logger.info(
                "path_d_shadow verdict memory=%s subject=%s target=%s predicate=%s decision=%s confidence=%.2f reason=%s",
                memory_id,
                subject,
                entry["target_memory_id"],
                entry["predicate"],
                entry["decision"],
                entry["confidence"],
                entry["reason"][:160],
            )

        logger.info(
            "path_d_shadow completed memory=%s subject=%s bridged=%d verdicts=%d ms=%d",
            memory_id,
            subject,
            len(bridged),
            len(summary["verdicts"]),
            int((time.monotonic() - t0) * 1000),
        )
        return summary
    except Exception:
        # Shadow mode must be invisible to the write pipeline — log and move on.
        logger.warning("path_d_shadow failed memory=%s", memory_id, exc_info=True)
        summary["skipped_reason"] = summary["skipped_reason"] or "error"
        return summary
