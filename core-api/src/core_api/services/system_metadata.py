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
- Caller input is sanitised at write time: the forgeable telemetry keys and
  the ``_system`` namespace itself are stripped — a caller cannot inject
  fake platform telemetry.
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
PLATFORM_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "llm_ms",
        "write_latency_ms",
        "semantic_dedup_ms",
        "business_relevance",
        "contains_pii",
        "pii_types",
        "write_mode",
        "enrichment_pending",
        "embedding_pending",
        "memory_type_agent_set",
        "pii_flagged_by",
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
