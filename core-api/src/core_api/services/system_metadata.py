"""C25 — the platform/caller metadata boundary.

The write pipeline has always stored its telemetry and enrichment output
(``llm_ms``, ``summary``, ``tags``, ``write_latency_ms`` …) directly in the
CALLER's ``metadata`` dict — undocumented and collision-prone: a caller
writing ``metadata={"summary": ...}`` was silently overwritten by enrichment,
and a caller-supplied ``llm_ms`` survived as fake telemetry whenever
enrichment didn't run (MemoryImpact C9 / AX-audit N8).

This module is the single registry of platform-written keys plus the helpers
every writer goes through:

- Platform values are written to BOTH the legacy top-level key (kept for one
  release so existing consumers see no change) AND the reserved
  ``metadata["_system"]`` namespace — EXCEPT when the caller owns the key
  (``summary`` / ``tags``): then the caller's value stays at top level and
  the platform's copy lives only under ``_system``.
- Caller input is sanitised at write time: every key in
  ``PLATFORM_ONLY_KEYS`` and the ``_system`` namespace itself are stripped —
  a caller cannot inject fake platform values. That set covers more than
  telemetry: governance verdicts, dedup outcomes and row lineage are all
  forgeable in exactly the same way, and were accepted from callers until
  the registry was widened to name them.
- Read-side, ``MemoryOut.system_metadata`` is derived from ``_system`` plus
  the legacy top-level keys, so historical rows (written before this module
  existed) expose the same view without a migration.
"""

from __future__ import annotations

from typing import Any

SYSTEM_NAMESPACE = "_system"

# Keys only the platform may write; stripped from caller input at write time.
# ``summary`` and ``tags`` are deliberately NOT here — callers legitimately
# own those; the platform's versions go to the ``_system`` namespace when the
# caller has set their own.
#
# Grouped by the step that writes them, so a key can be traced to its writer
# without grepping. The registry is the ONLY thing standing between a caller
# and a forged platform value: a platform key absent from this set is written
# by the platform and accepted from callers, which is the whole defect this
# grouping is meant to make visible.
#
# ``source`` is deliberately absent. The platform writes it ("auto_chunk",
# "atomic_fact_fanout") but so does ingest, in caller-adjacent item metadata —
# reserving it would strip that stamp the way M-48 nearly stripped
# ``memory_type_agent_set``. It is a shared key, not a platform-only one.
PLATFORM_ONLY_KEYS: frozenset[str] = frozenset(
    {
        # Write-path timing. Forgeable into fake latency/telemetry.
        "llm_ms",
        "write_latency_ms",
        "semantic_dedup_ms",
        "near_dup_check_ms",  # DetectNearDuplicate
        "dedup_judge_ms",  # CheckSemanticDuplicate
        "triple_emission_ms",  # EmitMemoryTriple
        # Governance verdicts. Forging these rewrites a compliance decision.
        "business_relevance",
        "contains_pii",
        "pii_types",
        "pii_flagged_by",
        "governance_llm_uncertain",  # GovernanceDecision
        "nonbusiness_kept_private",  # GovernanceDecision
        # Dedup / near-duplicate outcomes. ``near_duplicate_of`` is read back by
        # callers to decide whether to merge or undo, so a forged one redirects
        # a real decision.
        "dedup_skipped_reason",
        "dedup_candidate_similarity",
        "dedup_judge_confidence",
        "dedup_subject_preflight",
        "near_dup_skipped_reason",
        "near_duplicate_of",
        "near_duplicate_similarity",
        # Write mode and the pending flags consumers poll on.
        "write_mode",
        "enrichment_pending",
        "embedding_pending",
        # Provenance and lineage. Forging these fabricates where a row came from.
        "memory_type_agent_set",
        "weight_source",
        "parent_memory_id",  # auto-chunk + atomic-fact children
        "auto_chunked",
        "child_count",
        # Enrichment output.
        "retrieval_hint",
    }
)

# Caller-ownable keys the platform also produces.
CALLER_OWNABLE_KEYS: frozenset[str] = frozenset({"summary", "tags"})

# Everything the platform writes — the read-side extraction set.
PLATFORM_KEYS: frozenset[str] = PLATFORM_ONLY_KEYS | CALLER_OWNABLE_KEYS


def sanitize_caller_metadata(metadata: dict | None) -> dict:
    """Strip platform-reserved keys (and the namespace) from caller input.

    Returns a shallow copy; the caller's own keys — including ``summary`` /
    ``tags`` — pass through untouched.
    """
    if not metadata:
        return {}
    return {k: v for k, v in metadata.items() if k not in PLATFORM_ONLY_KEYS and k != SYSTEM_NAMESPACE}


def set_system_value(
    metadata: dict,
    key: str,
    value: Any,
    *,
    caller_keys: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Record one platform-written value.

    Always lands in ``metadata["_system"]``. Also mirrored to the legacy
    top-level key (one-release dual-write) UNLESS the caller owns that key —
    the clobber fix: a caller's own ``summary`` is never overwritten again.
    """
    metadata.setdefault(SYSTEM_NAMESPACE, {})[key] = value
    if not (key in CALLER_OWNABLE_KEYS and key in caller_keys):
        metadata[key] = value


def extract_system_metadata(metadata: dict | None) -> dict | None:
    """Read-side view: ``_system`` merged over legacy top-level platform keys.

    Works for historical rows (no ``_system``) and new rows alike; returns
    None when nothing platform-written is present so unenriched rows keep the
    field absent instead of ``{}``.
    """
    if not metadata:
        return None
    legacy = {k: metadata[k] for k in PLATFORM_KEYS if k in metadata}
    nested = metadata.get(SYSTEM_NAMESPACE) or {}
    merged = {**legacy, **nested}
    return merged or None
