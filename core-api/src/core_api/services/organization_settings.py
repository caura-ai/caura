"""Per-organization settings — storage + resolution.

Settings are stored as a JSONB blob in ``organization_settings`` (one row
per organization, overrides only). Every update additionally writes a flat
diff to ``organization_settings_audit`` for attribution and history.

Resolution order for any value:
    org override (cached) → global env default (``core_api.config.Settings``)
    → hardcoded Pydantic default

The function parameters here are still named ``tenant_id`` for call-site
back-compat (CAURA-654) — the value is treated as the org-key internally.
In OSS-standalone the tenant_id IS the org_id (single implicit org per
tenant); in enterprise callers should pass the actual org_id (parameter
rename to ``org_id`` is a follow-up that will touch ~20 call sites).

Reads go through a per-process ``TTLCache`` (5-min TTL). Writes invalidate
the local cache entry immediately; other workers catch up on TTL expiry.
Cross-worker invalidation is tracked as a follow-up (see CAURA-571).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cachetools import TTLCache
from croniter import CroniterBadCronError, croniter

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.lifecycle_purge_request import (
    MEMORY_RETENTION_MAX_DAYS,
    MEMORY_RETENTION_MIN_DAYS,
)
from common.events.org_settings_changed_event import OrgSettingsChangedEvent
from common.events.topics import Topics
from common.governance import PIICategory
from common.organization_settings_merge import deep_merge as _deep_merge
from common.provider_names import ProviderName
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as global_settings

logger = logging.getLogger(__name__)


# ── Settings schema defaults ──

DEFAULT_SETTINGS: dict = {
    "enrichment": {
        "provider": None,
        "model": None,
        "enabled": None,
    },
    "recall": {
        "provider": None,
        "model": None,
        "enabled": None,
    },
    "embedding": {
        "provider": None,
        "model": None,
    },
    "entity_extraction": {
        "provider": None,
        "model": None,
        "enabled": None,
    },
    "fallback_llm": {
        "provider": None,
        "model": None,
    },
    # Cached per-agent activity digest (nightly generation; served read-only by
    # GET /api/v1/reports/agent-activity). Opt-in — disabled by default because
    # generation spends LLM tokens. See core_api.services.agent_digest.
    "agent_digest": {
        "enabled": False,
        "cadence": "daily",  # daily | weekly | both
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "top_n": 25,
        "max_memories_per_agent": 60,
        "min_activity_threshold": 3,
        "max_cost_per_run_usd": 2.0,
        "retention_days": 90,
    },
    "search": {
        "recall_boost": None,
        "graph_retrieval": None,
        # Tenant-wide default search profile (A47). Any search_profile knob set
        # here (min_similarity, top_k, freshness_floor, ...) becomes the fallback
        # for EVERY agent in the tenant, filling the gap between a per-agent tuned
        # profile and the global constants. Empty by default → global constants
        # apply, unchanged. Validated by ``_validate_default_search_profile`` on
        # write (strict, raises) and ``validate_search_profile`` on read (clamps).
        "default_profile": {},
    },
    "crystallizer": {
        "auto_crystallize": None,
    },
    "dedup": {
        "semantic_dedup_enabled": None,
    },
    "lifecycle": {
        "lifecycle_automation_enabled": None,
        # Days to keep soft-deleted memories before they're physically
        # purged (CAURA-656). Daily cron reads this per-org and runs
        # ``purge-soft-deleted``. ``None`` means "use the global
        # default" (30 — see ResolvedConfig.memory_retention_days).
        # Range constrained to 1-30 by the validator below; the UI
        # numeric input mirrors that range.
        "memory_retention_days": None,
    },
    "entity_linking": {
        "auto_entity_linking_enabled": None,
    },
    # Periodic discovery insights (lifecycle-insights cron). Opt-in:
    # default is False because each tick runs an LLM reasoning pass
    # per fleet (or per tenant when fleet-less) and that cost is not
    # justified for every corpus. An org flips this to True when it
    # wants the daily ``generate_insights(focus='discover')`` pass.
    # The activity gate inside the consumer additionally skips ticks
    # where no non-insight memories have been written since the last
    # insights run, so even an enabled tenant pays only when the
    # corpus has grown.
    "insights": {
        "auto_insights_enabled": None,
    },
    "observability": {
        # Opt-in (default off). When on, each agent-chosen ``memclaw_recall``
        # call is logged (query + scope + candidate scores + below-floor
        # near-misses) to ``recall_event`` / ``recall_candidate`` for "why
        # aren't good memories recalled?" analysis.
        "recall_logging_enabled": None,
        # Opt-in (default off). When on, the plugin's automatic ``/search``
        # path is ALSO logged — but in a lighter form: returned candidates
        # only (id + scores), no below-floor near-misses, since ``/search``
        # is high-volume. Independent of ``recall_logging_enabled`` so it can
        # be enabled for a short diagnostic window on a couple of tenants and
        # then turned back off.
        "search_recall_logging_enabled": None,
        # Fraction (0.0-1.0) of ``/search`` events for which below-floor
        # near-misses ARE recorded (only relevant when
        # ``search_recall_logging_enabled`` is on). Default 0.0 = never (pure
        # returned-only light mode). Set e.g. 0.01 to keep near-misses on ~1%
        # of search events — enough to estimate the "just-missed" distribution
        # (was the good memory rank 7 at cosine 0.27?) without the full
        # candidate-row volume on the bulk path. Capped at the same
        # ``_NEAR_MISS_LIMIT`` per sampled event as ``mcp_recall``.
        "search_recall_near_miss_sample_rate": None,
    },
    "chunking": {
        "auto_chunk_enabled": None,
    },
    "write": {
        "default_write_mode": None,  # None = "fast"; "fast" | "strong"
        # CAURA-123 — RDF triple emission. When true (the default), a
        # pre-write step extracts (subject_entity_id, predicate,
        # object_value) from the request so the deterministic RDF
        # contradiction path (contradiction_detector.py) can fire
        # instead of falling through to the LLM. ``None`` resolves to
        # the global default (true).
        "triple_emission_enabled": None,
        # CAURA-130 (L3.8) — Path C retraction kill-switch. When true
        # (the default), Path C's ``_attempt_path_c_retraction`` runs
        # the entity-aware judge and may revert a Path A verdict. When
        # false, Path C skips retraction entirely and Path A's verdict
        # stands. Ops escape valve for tenants whose retraction
        # misbehaves; flip per-tenant without a deploy.
        "retraction_enabled": None,
    },
    "agents": {
        "require_agent_approval": None,
    },
    # CAURA-444 — plugin auto-upgrade. When `auto_upgrade_enabled` is
    # true (the default), the heartbeat handler queues a `deploy`
    # command for any node whose `plugin_version` is older than
    # `MIN_RECOMMENDED_PLUGIN_VERSION` (version_compat.py).
    # Per-tenant flip allows operators to opt out.
    #
    # The `KNOWN_BROKEN_DEPLOY_VERSIONS` denylist (in routes/fleet.py)
    # is a separate global guard that prevents auto-deploy specifically
    # for plugin versions whose deploy machinery is itself broken
    # (currently: 2.3.0 — drift in srcFiles + missing version-stamp).
    "memclaw": {
        "auto_upgrade_enabled": None,  # None = use global default (true)
    },
    "security_audit": {
        "schedule_enabled": None,
        "schedule_cron": None,
        "alerts_enabled": None,
        "alert_recipients": None,
        "alert_score_below": None,
        "alert_critical_findings_min": None,
        "alert_score_drop_delta": None,
    },
    # Skill Factory SF-006 — per-tenant knobs for the lake-side skill
    # production pipeline (Forge resident + HITL Inbox + Sentinel scan +
    # harness install). Defaults are CONCRETE here (not None) so the
    # OSS resolver and tests have predictable values; tenants override
    # by writing a partial dict (existing _deep_merge + _check_keys
    # plumbing). See docs/live-memory-pitch/skill-factory-implementation-plan.md §12.
    "skills_factory": {
        # Feature flag gating the SF-002 ``memclaw_doc`` skills-write
        # adjustments. OSS default ``False`` so existing eToro and
        # caura-dev-fleet tenants see ZERO behavior change until they
        # explicitly opt in. Phase 0 ships the plumbing; per-tenant
        # rollout flips this true.
        "enabled": False,
        # Hard caps. ``_check_keys`` and ``_LEAF_TYPES`` enforce shape;
        # the routes/documents.py write path enforces values.
        "description_max_bytes": 160,
        "body_max_bytes": 40_000,
        "inbox_max_pending": 50,
        # Days a rejected cluster_fingerprint stays poison-flagged in
        # forge_rejected_fingerprints before Forge may re-propose it.
        "rejection_cooloff_days": 30,
        # Sentinel scanner behavior. ``fail_on_critical=true`` → any
        # critical finding flips the doc to ``status=quarantined``
        # instead of letting it surface in the inbox.
        "sentinel": {
            "fail_on_critical": True,
            # When True, a Forge candidate that passes ALL six
            # auto-gates AND carries a clean Sentinel scan
            # (``scan.state='clean'``, ``critical=0``) is promoted
            # straight to ``status='active'`` — skipping the HITL
            # Inbox approve step. Default False keeps the human in
            # the loop. Flipping this true means the tenant TRUSTS
            # the Sentinel scanner as the sole gate before a skill
            # goes live; dirty / quarantined / warn-only candidates
            # still route to ``staged`` and require human review.
            "auto_promote_clean": False,
        },
        # Forge resident knobs. Phase 0 publishes the topic + stub
        # handler; Phase 1 lands the real worker that reads these.
        # ``min_cluster_size`` default 3 is the demo value (plan §5);
        # production tenants flip to 10 once outcome volume justifies.
        "forge": {
            "cron_interval_hours": 6,
            "min_cluster_size": 3,
            "min_distinct_agents": 3,
            "freshness_window_days": 14,
            "llm_tokens_per_run": 50_000,
            "max_writes_per_run": 20,
        },
        # OpenClaw PROPOSAL.md bridge (Phase 5). Default OFF — turning
        # it on only matters once the OpenClaw workspace emitter ships.
        "openclaw_bridge": {
            "enabled": False,
        },
    },
    "entity_blocklist": [
        "team",
        "meeting",
        "project",
        "system",
        "process",
        "approach",
        "update",
        "issue",
        "change",
        "result",
        "group",
        "company",
        "person",
        "user",
        "client",
        "thing",
        "stuff",
        "idea",
        "work",
        "code",
    ],
    # Ingestion-boundary content governance (eToro). Opt-in: booleans default
    # False and the action/disposition default to the safe, non-destructive
    # choice (flag / store) so enabling the feature later is a deliberate step.
    # ``pii.categories`` toggles which detector categories are in scope; when
    # PII is enabled with NO category selected, the gate scans ALL categories
    # (the secure default — enabling protection shouldn't silently protect
    # nothing). See ``ResolvedConfig.governance_pii``.
    #
    # Two paths back the PII ``action`` (mask/drop/flag), with different recall:
    #   1. Deterministic, span-aware validators (``GovernanceScanContent``):
    #      regex/Luhn/IBAN/entropy. The strong, fail-closed path for structured
    #      PII (email/phone/cards/IBAN/keys), and the only one that can honestly
    #      ``mask`` (it has span offsets). Precision confirmed; high recall on
    #      the patterns it covers.
    #   2. The enrichment LLM's free-form ``contains_pii`` signal
    #      (``GovernanceDecision``): catches unstructured/contextual PII the
    #      patterns can't (e.g. "X is in addiction recovery"), but its RECALL is
    #      bounded by the enrichment model — a small/cheap model (e.g. *-nano)
    #      under-detects subtly-phrased free-form PII. A tenant relying on
    #      ``drop``/``mask`` to catch FREE-FORM PII should set a capable
    #      ``enrichment.model``; the deterministic path stays the precise
    #      backstop, but free-form coverage is only as strong as that model's
    #      recall. (The LLM path has no offsets, so a ``mask`` policy can only
    #      flag a free-form match — see ``llm_pii_audit_detail`` /
    #      ``configured_action``.)
    "governance": {
        "pii": {
            "enabled": False,
            "action": None,  # None → "flag"; one of mask | drop | flag
            "categories": {
                "email": False,
                "phone": False,
                "credit_card": False,
                "iban": False,
                "national_id": False,
                "api_key": False,
                "secret": False,
            },
        },
        "non_business": {
            "enabled": False,
            "disposition": None,  # None → "store"; one of drop | keep_private | store
            # Fast pre-gate (opt-in): a cheap business-vs-personal go/no-go that
            # runs BEFORE enrichment / embedding / entity extraction and rejects
            # personal content early when ``disposition="drop"``. Disabled by
            # default like every other security control. Its own provider/model
            # so the signal is independent of the enrichment provider (survives
            # ``enrichment_provider=none``, e.g. CI). ``min_confidence`` None →
            # act on any "personal" verdict. Raising it makes the pre-gate SKIP
            # the early drop for low-confidence "personal" verdicts and defer
            # them to the more accurate post-enrichment gate; it does NOT add a
            # confidence floor to the final decision (that backstop drops any
            # "personal" verdict unconditionally — there is no enrichment
            # confidence to gate on). So it trades a little extra compute for
            # fewer *early* rejects on borderline content, not a blanket
            # reduction in drops. ``fail_closed`` False → fail-open: a classifier
            # failure/timeout never blocks a write (the post-enrichment gate
            # remains the backstop). Set ``fail_closed`` True so a tenant that
            # requires enforcement REJECTS writes (503) when the classifier is
            # unavailable rather than storing unclassified content.
            "pregate": {
                "enabled": False,
                "provider": None,
                "model": None,
                "min_confidence": None,
                "fail_closed": False,
            },
        },
    },
    "api_keys": {},
}

# Keys are ``ProviderName`` enum values (.value) so a typo here is caught
# at import time rather than silently producing an entry UI that no tenant
# can select.
PROVIDER_OPTIONS = {
    "enrichment": {
        ProviderName.OPENAI.value: ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-4.1-nano", "gpt-4o-mini"],
        ProviderName.GEMINI.value: [
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "recall": {
        ProviderName.OPENAI.value: ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-4.1-nano", "gpt-4o-mini"],
        ProviderName.GEMINI.value: [
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "embedding": {
        ProviderName.OPENAI.value: ["text-embedding-3-small", "text-embedding-3-large"],
    },
    "entity_extraction": {
        ProviderName.OPENAI.value: ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-4.1-nano", "gpt-4o-mini"],
        ProviderName.GEMINI.value: [
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "fallback_llm": {
        ProviderName.OPENAI.value: ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-4.1-nano", "gpt-4o-mini"],
        ProviderName.GEMINI.value: [
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
        ProviderName.ANTHROPIC.value: ["claude-haiku-4-5-20251001"],
        ProviderName.OPENROUTER.value: ["openai/gpt-5.4-nano", "openai/gpt-4.1-nano"],
    },
}


def _remap_vertex(provider: str) -> str:
    """Remap deprecated tenant-tier ``vertex`` provider to ``openai``.

    Existing tenants with ``provider="vertex"`` saved in DB settings hit
    ``ValueError`` on every LLM call after the tenant-tier removal.
    ``call_with_fallback`` catches those but logs misleadingly and may
    silently drop to FakeLLMProvider for GCP-only tenants. Remap at the
    read-side so stored settings degrade gracefully.
    """
    if provider == "vertex":
        logger.warning(
            "Tenant has provider='vertex' in stored settings; "
            "vertex is platform-tier only. Remapping to 'openai'."
        )
        return "openai"
    return provider


# ── TTL cache: org_id → settings dict ──
#
# Per-process cache; each uvicorn worker has its own. Staleness across workers
# is bounded by the TTL (5 min). Writes on the current worker invalidate
# locally; others catch up on expiry. See CAURA-571 for cross-worker NOTIFY.
#
# No locking: cache misses may issue duplicate DB reads under concurrency, but
# the query is an indexed PK lookup and the result is identical, so racing
# populations are harmless.
_settings_cache: TTLCache[str, dict] = TTLCache(maxsize=10_000, ttl=300)


def _validate_cron(expr: str) -> None:
    """Raise ``ValueError`` if *expr* is not a valid cron expression."""
    try:
        croniter(expr)
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression {expr!r}: {exc}") from exc


_PII_ACTIONS = frozenset({"mask", "drop", "flag"})
_NON_BUSINESS_DISPOSITIONS = frozenset({"drop", "keep_private", "store"})
# The fast pre-gate accepts any known LLM provider name (incl. ``none``/``fake``
# for disable/test). Membership-checked so a typo can't silently disable the gate.
_PREGATE_PROVIDERS = frozenset(p.value for p in ProviderName)


def _validate_governance_enums(payload: dict) -> None:
    """Raise ``ValueError`` for governance enum values outside their allowed set.

    ``_validate_leaf_types`` already pins these to ``str``; this pins the
    actual allowed values (the leaf-type machinery checks Python types, not
    value membership).
    """
    gov = payload.get("governance")
    if not isinstance(gov, dict):
        return
    action = gov.get("pii", {}).get("action")
    if action is not None and action not in _PII_ACTIONS:
        raise ValueError(f"governance.pii.action must be one of {sorted(_PII_ACTIONS)}, got {action!r}")
    nb = gov.get("non_business", {})
    disposition = nb.get("disposition")
    if disposition is not None and disposition not in _NON_BUSINESS_DISPOSITIONS:
        raise ValueError(
            f"governance.non_business.disposition must be one of "
            f"{sorted(_NON_BUSINESS_DISPOSITIONS)}, got {disposition!r}"
        )
    pregate = nb.get("pregate", {})
    provider = pregate.get("provider")
    if provider is not None and provider not in _PREGATE_PROVIDERS:
        raise ValueError(
            f"governance.non_business.pregate.provider must be one of "
            f"{sorted(_PREGATE_PROVIDERS)}, got {provider!r}"
        )
    min_conf = pregate.get("min_confidence")
    if min_conf is not None and not (0.0 <= min_conf <= 1.0):
        raise ValueError(
            f"governance.non_business.pregate.min_confidence must be in [0.0, 1.0], got {min_conf!r}"
        )


def _validate_default_search_profile(payload: dict) -> None:
    """Strictly validate the tenant-wide ``search.default_profile`` on write.

    Unlike ``validate_search_profile`` (which silently clamps/drops for the
    agent-tune path), an org-wide setting write should fail loudly so an
    operator gets a 422 rather than a value that was quietly clamped. Unknown
    keys, wrong types, and out-of-range values all raise. Keys and ranges are
    the same source of truth as agent profiles (``_SEARCH_PROFILE_RULES``).
    """
    dp = payload.get("search", {}).get("default_profile")
    if dp is None:
        return
    if not isinstance(dp, dict):
        raise ValueError("search.default_profile must be an object")
    for key, value in dp.items():
        if key not in _SEARCH_PROFILE_RULES:
            raise ValueError(
                f"search.default_profile: unknown key {key!r} (allowed: {sorted(_SEARCH_PROFILE_RULES)})"
            )
        expected_type, (lo, hi), _ = _SEARCH_PROFILE_RULES[key]
        # Accept an int where a float is expected (e.g. min_similarity=0 → 0.0),
        # but never a bool (bool is an int subclass and would slip through).
        if expected_type is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        wrong_bool = isinstance(value, bool) and expected_type is not bool
        if wrong_bool or not isinstance(value, expected_type):
            raise ValueError(
                f"search.default_profile.{key} must be {expected_type.__name__}, got {type(value).__name__}"
            )
        if value < lo or value > hi:
            raise ValueError(f"search.default_profile.{key} must be in [{lo}, {hi}], got {value}")


def _check_keys(payload: dict, schema: dict, path: str = "") -> None:
    """Raise ``ValueError`` for any key in *payload* not present in *schema*.

    Recurses into nested dicts so sub-keys are also validated.
    """
    unknown = set(payload) - set(schema)
    if unknown:
        prefix = f"{path}." if path else ""
        raise ValueError(f"Unknown settings key(s): {sorted(prefix + k for k in unknown)}")
    for k, v in payload.items():
        schema_v = schema.get(k)
        if isinstance(schema_v, dict):
            if not isinstance(v, dict):
                full_key = f"{path}.{k}" if path else k
                raise ValueError(f"Settings key {full_key!r} must be an object, got {type(v).__name__}")
            if schema_v:
                _check_keys(v, schema_v, path=f"{path}.{k}" if path else k)


# Expected Python types for leaf values that need validation beyond key presence.
# Dotted paths match the nested structure in DEFAULT_SETTINGS.
_LEAF_TYPES: dict[str, type | tuple[type, ...]] = {
    "security_audit.schedule_enabled": bool,
    "security_audit.schedule_cron": str,
    "security_audit.alerts_enabled": bool,
    "security_audit.alert_recipients": list,
    "security_audit.alert_score_below": (int, float),
    "security_audit.alert_critical_findings_min": int,
    "security_audit.alert_score_drop_delta": (int, float),
    "search.recall_boost": bool,
    "search.graph_retrieval": bool,
    "crystallizer.auto_crystallize": bool,
    "dedup.semantic_dedup_enabled": bool,
    "lifecycle.lifecycle_automation_enabled": bool,
    "lifecycle.memory_retention_days": int,
    "entity_linking.auto_entity_linking_enabled": bool,
    "insights.auto_insights_enabled": bool,
    "observability.recall_logging_enabled": bool,
    "observability.search_recall_logging_enabled": bool,
    "observability.search_recall_near_miss_sample_rate": (int, float),
    "chunking.auto_chunk_enabled": bool,
    "agents.require_agent_approval": bool,
    "entity_blocklist": list,
    "memclaw.auto_upgrade_enabled": bool,
    "write.triple_emission_enabled": bool,
    "write.retraction_enabled": bool,
    # Skill Factory SF-006 — type validators for the skills_factory namespace.
    "skills_factory.enabled": bool,
    "skills_factory.description_max_bytes": int,
    "skills_factory.body_max_bytes": int,
    "skills_factory.inbox_max_pending": int,
    "skills_factory.rejection_cooloff_days": int,
    "skills_factory.sentinel.fail_on_critical": bool,
    "skills_factory.sentinel.auto_promote_clean": bool,
    "skills_factory.forge.cron_interval_hours": int,
    "skills_factory.forge.min_cluster_size": int,
    "skills_factory.forge.min_distinct_agents": int,
    "skills_factory.forge.freshness_window_days": int,
    "skills_factory.forge.llm_tokens_per_run": int,
    "skills_factory.forge.max_writes_per_run": int,
    "skills_factory.openclaw_bridge.enabled": bool,
    # Governance content policy (eToro). Enum values (action / disposition) are
    # type-checked here as str; their allowed values are checked by
    # ``_validate_governance_enums`` in update_settings.
    "governance.pii.enabled": bool,
    "governance.pii.action": str,
    "governance.pii.categories.email": bool,
    "governance.pii.categories.phone": bool,
    "governance.pii.categories.credit_card": bool,
    "governance.pii.categories.iban": bool,
    "governance.pii.categories.national_id": bool,
    "governance.pii.categories.api_key": bool,
    "governance.pii.categories.secret": bool,
    "governance.non_business.enabled": bool,
    "governance.non_business.disposition": str,
    "governance.non_business.pregate.enabled": bool,
    "governance.non_business.pregate.provider": str,
    "governance.non_business.pregate.model": str,
    "governance.non_business.pregate.min_confidence": (int, float),
    "governance.non_business.pregate.fail_closed": bool,
}

# Inclusive range constraints applied AFTER type validation. Listed
# separately rather than encoded in ``_LEAF_TYPES`` so types stay
# Python-class types (cleanly testable with ``isinstance``). Range
# constants are imported from the publisher-side payload so a future
# widening only needs to touch one source of truth.
_LEAF_RANGES: dict[str, tuple[int, int]] = {
    "lifecycle.memory_retention_days": (
        MEMORY_RETENTION_MIN_DAYS,
        MEMORY_RETENTION_MAX_DAYS,
    ),
    # ``rejection_cooloff_days`` must be >= 1: the poison-table
    # writer (``services/forge/poison.py:write_rejected_fingerprint``)
    # raises ValueError on < 1, which the inbox reject endpoint now
    # surfaces as 422. Capping at 365 prevents a tenant from
    # accidentally writing a near-permanent poison entry.
    "skills_factory.rejection_cooloff_days": (1, 365),
    # Size caps must be > 0: a tenant misconfiguring these to 0 or
    # negative would silently break ALL skills writes (every doc
    # would trip BODY_TOO_LARGE / DESCRIPTION_TOO_LARGE in Sentinel's
    # size check). Upper bounds chosen well above any realistic
    # SKILL.md (10 MB body, 10 KB description) — high enough that
    # legitimate tenants never hit them, low enough that an operator
    # typo can't pin a DoS-shaped write through the validator.
    "skills_factory.body_max_bytes": (1, 10_000_000),
    "skills_factory.description_max_bytes": (1, 10_000),
}


def _validate_leaf_types(payload: dict, prefix: str = "") -> None:
    """Raise ``ValueError`` if any leaf value has the wrong Python type
    or falls outside its declared inclusive range.
    """
    for k, v in payload.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            _validate_leaf_types(v, prefix=f"{path}.")
        elif v is not None and path in _LEAF_TYPES:
            expected = _LEAF_TYPES[path]
            # Python's ``bool`` is a subclass of ``int``, so a payload
            # like ``{"memory_retention_days": true}`` silently passes
            # the isinstance check on int-typed fields and then falls
            # through to the range check with a confusing "must be in
            # [1, 30], got True" message. Treat bool as a type
            # mismatch unless the field's declared type explicitly
            # includes bool.
            expected_types = expected if isinstance(expected, tuple) else (expected,)
            wrong_bool = isinstance(v, bool) and bool not in expected_types
            if wrong_bool or not isinstance(v, expected_types):
                type_name = (
                    expected.__name__
                    if isinstance(expected, type)
                    else " or ".join(t.__name__ for t in expected)
                )
                raise ValueError(f"Settings key {path!r} must be {type_name}, got {type(v).__name__}")
            if path in _LEAF_RANGES:
                lo, hi = _LEAF_RANGES[path]
                if not (lo <= v <= hi):
                    raise ValueError(f"Settings key {path!r} must be in [{lo}, {hi}], got {v!r}")


_PII_CATEGORY_VALUES: frozenset[str] = frozenset(c.value for c in PIICategory)


@dataclass(frozen=True)
class _GovPII:
    """Resolved PII governance policy. ``enabled_categories=None`` means scan
    ALL categories (the secure default when the feature is on but no category
    was narrowed); otherwise scan only the listed ones."""

    enabled: bool
    action: str  # "mask" | "drop" | "flag"
    enabled_categories: frozenset[PIICategory] | None


@dataclass(frozen=True)
class _GovNB:
    """Resolved non-business (personal-content) governance policy."""

    enabled: bool
    disposition: str  # "drop" | "keep_private" | "store"


@dataclass(frozen=True)
class _GovNBPregate:
    """Resolved fast pre-gate policy: a business/personal go/no-go before
    enrichment. ``provider``/``model`` None → resolved by the step (falls back to
    the enrichment provider). ``min_confidence`` None → act on any "personal"
    verdict; raising it defers low-confidence verdicts to the post-enrichment
    backstop rather than dropping them early (it does not floor the final drop).
    ``fail_closed`` → reject the write (503) when the classifier is unavailable
    instead of failing open."""

    enabled: bool
    provider: str | None
    model: str | None
    min_confidence: float | None
    fail_closed: bool


class ResolvedConfig:
    """Resolves LLM/feature config from organization overrides + global fallbacks."""

    def __init__(
        self,
        org_settings: dict | None = None,
        tenant_settings: dict | None = None,
    ):
        # ``tenant_settings`` is a back-compat alias for callers that
        # still pass the pre-CAURA-654 keyword. Silently absorbs them
        # rather than raising TypeError; consistent with the module
        # docstring's promise to keep call-site signatures stable until
        # the parameter rename follow-up lands.
        self._ts = org_settings or tenant_settings or {}

    # Governance (eToro content policy)
    @property
    def governance_pii(self) -> _GovPII:
        g = self._ts.get("governance", {}).get("pii", {})
        cats = g.get("categories", {})
        selected = frozenset(
            PIICategory(name) for name, on in cats.items() if on and name in _PII_CATEGORY_VALUES
        )
        return _GovPII(
            enabled=bool(g.get("enabled", False)),
            action=g.get("action") or "flag",
            # Empty selection → None → scan all categories (secure default).
            enabled_categories=selected or None,
        )

    @property
    def governance_non_business(self) -> _GovNB:
        g = self._ts.get("governance", {}).get("non_business", {})
        return _GovNB(
            enabled=bool(g.get("enabled", False)),
            disposition=g.get("disposition") or "store",
        )

    @property
    def governance_non_business_pregate(self) -> _GovNBPregate:
        g = self._ts.get("governance", {}).get("non_business", {}).get("pregate", {})
        return _GovNBPregate(
            enabled=bool(g.get("enabled", False)),
            provider=g.get("provider") or None,
            model=g.get("model") or None,
            min_confidence=g.get("min_confidence"),
            fail_closed=bool(g.get("fail_closed", False)),
        )

    # Enrichment
    @property
    def enrichment_provider(self) -> str:
        return _remap_vertex(
            self._ts.get("enrichment", {}).get("provider") or global_settings.entity_extraction_provider
        )

    @property
    def enrichment_model(self) -> str:
        return self._ts.get("enrichment", {}).get("model") or global_settings.entity_extraction_model

    @property
    def enrichment_enabled(self) -> bool:
        val = self._ts.get("enrichment", {}).get("enabled")
        if val is not None:
            return val
        return global_settings.use_llm_for_memory_creation

    # Recall
    @property
    def recall_provider(self) -> str:
        return _remap_vertex(
            self._ts.get("recall", {}).get("provider") or global_settings.entity_extraction_provider
        )

    @property
    def recall_model(self) -> str:
        return self._ts.get("recall", {}).get("model") or global_settings.entity_extraction_model

    @property
    def recall_enabled(self) -> bool:
        val = self._ts.get("recall", {}).get("enabled")
        if val is not None:
            return val
        return global_settings.use_llm_for_memory_creation

    # Embedding
    @property
    def embedding_provider(self) -> str:
        return self._ts.get("embedding", {}).get("provider") or global_settings.embedding_provider

    @property
    def embedding_model(self) -> str | None:
        return self._ts.get("embedding", {}).get("model")

    # Entity extraction
    @property
    def entity_extraction_provider(self) -> str:
        return _remap_vertex(
            self._ts.get("entity_extraction", {}).get("provider")
            or global_settings.entity_extraction_provider
        )

    @property
    def entity_extraction_model(self) -> str:
        return self._ts.get("entity_extraction", {}).get("model") or global_settings.entity_extraction_model

    @property
    def entity_extraction_enabled(self) -> bool:
        val = self._ts.get("entity_extraction", {}).get("enabled")
        if val is not None:
            return val
        return global_settings.entity_extraction_provider != ProviderName.NONE

    # Fallback LLM
    @property
    def fallback_llm_provider(self) -> str | None:
        return self._ts.get("fallback_llm", {}).get("provider")

    @property
    def fallback_llm_model(self) -> str | None:
        return self._ts.get("fallback_llm", {}).get("model")

    def resolve_fallback(self) -> tuple[str | None, str | None]:
        provider = self.fallback_llm_provider
        model = self.fallback_llm_model
        if provider:
            return provider, model
        primary = self.enrichment_provider
        candidates = [
            (ProviderName.OPENAI.value, self.openai_api_key),
            (ProviderName.ANTHROPIC.value, self.anthropic_api_key),
            (ProviderName.GEMINI.value, self.gemini_api_key),
            (ProviderName.OPENROUTER.value, self.openrouter_api_key),
        ]
        for prov, key in candidates:
            if prov != primary and key:
                return prov, model
        return None, None

    # API keys (from global config only in OSS)
    @property
    def openai_api_key(self) -> str | None:
        return self._ts.get("api_keys", {}).get("openai_api_key") or global_settings.openai_api_key

    @property
    def anthropic_api_key(self) -> str | None:
        return self._ts.get("api_keys", {}).get("anthropic_api_key") or global_settings.anthropic_api_key

    @property
    def openrouter_api_key(self) -> str | None:
        return self._ts.get("api_keys", {}).get("openrouter_api_key") or global_settings.openrouter_api_key

    @property
    def gemini_api_key(self) -> str | None:
        return self._ts.get("api_keys", {}).get("gemini_api_key") or global_settings.gemini_api_key

    # Search
    @property
    def recall_boost(self) -> bool:
        val = self._ts.get("search", {}).get("recall_boost")
        return val if val is not None else True

    @property
    def graph_expand(self) -> bool:
        val = self._ts.get("search", {}).get("graph_retrieval")
        return val if val is not None else True

    @property
    def default_search_profile(self) -> dict:
        """Tenant-wide default search profile (A47).

        Sits below a per-agent tuned profile and above the global constants in
        ``resolve_search_profile``. Sanitised via ``validate_search_profile`` on
        read so a malformed stored value can never crash the search pipeline —
        unknown/out-of-range knobs are clamped or dropped. Empty ⇒ constants.
        """
        return validate_search_profile(self._ts.get("search", {}).get("default_profile", {}) or {})

    # Crystallizer
    @property
    def auto_crystallize_enabled(self) -> bool:
        val = self._ts.get("crystallizer", {}).get("auto_crystallize")
        return val if val is not None else True

    # Dedup
    @property
    def semantic_dedup_enabled(self) -> bool:
        val = self._ts.get("dedup", {}).get("semantic_dedup_enabled")
        return val if val is not None else True

    # Lifecycle
    @property
    def lifecycle_automation_enabled(self) -> bool:
        val = self._ts.get("lifecycle", {}).get("lifecycle_automation_enabled")
        return val if val is not None else True

    @property
    def memory_retention_days(self) -> int:
        """Days to keep soft-deleted memories before they're purged
        (CAURA-656). Default 30 matches the UI numeric input's upper
        bound — generous on the safe side; an org tightens it down to
        as low as 1 day if their compliance posture demands it. The
        validator on settings PUT already constrains the override to
        [1, 30].
        """
        val = self._ts.get("lifecycle", {}).get("memory_retention_days")
        return val if val is not None else 30

    # Entity linking
    @property
    def auto_entity_linking_enabled(self) -> bool:
        val = self._ts.get("entity_linking", {}).get("auto_entity_linking_enabled")
        return val if val is not None else True

    # Insights — opt-in (default False). Sibling of auto_crystallize /
    # auto_entity_linking but with the inverse default because the
    # discovery LLM pass is expensive and not universally useful.
    @property
    def auto_insights_enabled(self) -> bool:
        val = self._ts.get("insights", {}).get("auto_insights_enabled")
        return val if val is not None else False

    # Recall logging — opt-in (default False). When on, agent-chosen
    # ``memclaw_recall`` calls are logged to recall_event / recall_candidate.
    @property
    def recall_logging_enabled(self) -> bool:
        val = self._ts.get("observability", {}).get("recall_logging_enabled")
        return val if val is not None else False

    # Search-path recall logging — opt-in (default False). When on, the
    # automatic ``/search`` path is logged too (lighter: returned-only, no
    # near-misses). Independent of ``recall_logging_enabled``.
    @property
    def search_recall_logging_enabled(self) -> bool:
        val = self._ts.get("observability", {}).get("search_recall_logging_enabled")
        return val if val is not None else False

    # Fraction of ``/search`` events that keep below-floor near-misses
    # (default 0.0). Clamped to [0.0, 1.0]. Only consulted when
    # ``search_recall_logging_enabled`` is on.
    @property
    def search_recall_near_miss_sample_rate(self) -> float:
        val = self._ts.get("observability", {}).get("search_recall_near_miss_sample_rate")
        if val is None:
            return 0.0
        return max(0.0, min(1.0, float(val)))

    # Chunking
    @property
    def auto_chunk_enabled(self) -> bool:
        val = self._ts.get("chunking", {}).get("auto_chunk_enabled")
        return val if val is not None else False

    # Entity blocklist
    @property
    def entity_blocklist(self) -> frozenset[str]:
        custom = self._ts.get("entity_blocklist")
        if custom is not None:
            # Lower-case normalisation so this is symmetric with the
            # ``name.lower() not in bl`` check in
            # ``entity_extraction_worker._is_valid_entity``. Tenant-
            # supplied entries with mixed case (``"Team"``,
            # ``"SYSTEM"``) would otherwise silently miss the filter.
            return frozenset(entry.lower() for entry in custom)
        return frozenset(DEFAULT_SETTINGS["entity_blocklist"])

    # Write mode
    @property
    def default_write_mode(self) -> str:
        val = self._ts.get("write", {}).get("default_write_mode")
        if val in ("fast", "strong"):
            return val
        return "fast"  # default to fast when unset

    @property
    def triple_emission_enabled(self) -> bool:
        # CAURA-123 — default ON. Tenants can opt out per-org without
        # a deploy (instant rollback path).
        val = self._ts.get("write", {}).get("triple_emission_enabled")
        return bool(val) if val is not None else True

    @property
    def retraction_enabled(self) -> bool:
        # CAURA-130 (L3.8) — default ON. Per-tenant kill-switch for
        # Path C's retraction phase. Flip to False to leave Path A's
        # verdict in place unconditionally for this tenant; useful as
        # an ops escape valve if a tenant's retraction misbehaves.
        val = self._ts.get("write", {}).get("retraction_enabled")
        return bool(val) if val is not None else True

    # Agents
    @property
    def require_agent_approval(self) -> bool:
        val = self._ts.get("agents", {}).get("require_agent_approval")
        return bool(val) if val is not None else False

    # Security audit
    @property
    def security_audit_schedule_enabled(self) -> bool:
        val = self._ts.get("security_audit", {}).get("schedule_enabled")
        if val is not None:
            return bool(val)
        return global_settings.security_audit_schedule_enabled

    @property
    def security_audit_schedule_cron(self) -> str:
        val = self._ts.get("security_audit", {}).get("schedule_cron")
        if val is not None:
            return val
        return global_settings.security_audit_schedule_cron

    @property
    def security_audit_alerts_enabled(self) -> bool:
        val = self._ts.get("security_audit", {}).get("alerts_enabled")
        if val is not None:
            return bool(val)
        return global_settings.security_audit_alerts_enabled

    @property
    def security_audit_alert_recipients(self) -> list[str]:
        val = self._ts.get("security_audit", {}).get("alert_recipients")
        if val is not None:
            if isinstance(val, str):
                return [val] if val else []
            return list(val)
        return list(global_settings.security_audit_alert_recipients)

    @property
    def security_audit_alert_score_below(self) -> float | None:
        val = self._ts.get("security_audit", {}).get("alert_score_below")
        if val is not None:
            return val
        return global_settings.security_audit_alert_score_below

    @property
    def security_audit_alert_critical_findings_min(self) -> int | None:
        val = self._ts.get("security_audit", {}).get("alert_critical_findings_min")
        if val is not None:
            return val
        return global_settings.security_audit_alert_critical_findings_min

    @property
    def security_audit_alert_score_drop_delta(self) -> float | None:
        val = self._ts.get("security_audit", {}).get("alert_score_drop_delta")
        if val is not None:
            return val
        return global_settings.security_audit_alert_score_drop_delta


# Search profile validation
_SEARCH_PROFILE_RULES: dict[str, tuple[type, tuple, object]] = {
    "top_k": (int, (1, 20), None),
    "min_similarity": (float, (0.1, 0.9), None),
    "fts_weight": (float, (0.0, 1.0), None),
    "freshness_floor": (float, (0.0, 1.0), None),
    "freshness_decay_days": (int, (7, 730), None),
    "recall_boost_cap": (float, (1.0, 3.0), None),
    "recall_decay_window_days": (int, (7, 365), None),
    "graph_max_hops": (int, (0, 5), None),
    "similarity_blend": (float, (0.0, 1.0), None),
}


def validate_search_profile(profile: dict) -> dict:
    """Validate and sanitise a search_profile dict."""
    if not profile:
        return {}

    cleaned: dict = {}
    for key, value in profile.items():
        if key not in _SEARCH_PROFILE_RULES:
            cleaned[key] = value
            continue

        expected_type, (lo, hi), default = _SEARCH_PROFILE_RULES[key]

        if expected_type is float and isinstance(value, int):
            value = float(value)

        if not isinstance(value, expected_type):
            logger.warning(
                "search_profile key '%s' has wrong type %s (expected %s), using default",
                key,
                type(value).__name__,
                expected_type.__name__,
            )
            if default is not None:
                cleaned[key] = default
            continue

        if value < lo or value > hi:
            clamped = max(lo, min(hi, value))
            logger.warning(
                "search_profile key '%s' value %s out of range [%s, %s], clamped to %s",
                key,
                value,
                lo,
                hi,
                clamped,
            )
            cleaned[key] = clamped
            continue

        cleaned[key] = value

    return cleaned


# ── Storage-backed read/write ──


def invalidate_cache(tenant_id: str) -> None:
    """Evict a tenant's cached settings. Exposed for tests + future NOTIFY hook."""
    _settings_cache.pop(tenant_id, None)
    logger.info("organization_settings cache invalidated for %s", tenant_id)


async def resolve_config(tenant_id: str) -> ResolvedConfig:
    """Resolve config for a tenant: tenant override → global env default.

    Settings load through core-storage-api (Fix 2 Phase 0).
    """
    raw = await get_raw_settings(tenant_id)
    return ResolvedConfig(raw)


async def get_raw_settings(tenant_id: str) -> dict:
    """Return the tenant's raw override dict, or ``{}`` if no overrides set.

    Cache-first (5-min TTL); on a miss, fetched via core-storage-api (Fix 2
    Phase 0 routed this through the storage client — no direct DB read).
    """
    cached = _settings_cache.get(tenant_id)
    if cached is not None:
        logger.debug("organization_settings cache hit for %s", tenant_id)
        return cached
    return await _load_and_cache(tenant_id)


async def _load_and_cache(tenant_id: str) -> dict:
    resolved = await get_storage_client().get_org_settings(tenant_id)
    _settings_cache[tenant_id] = resolved
    logger.info("organization_settings cache miss for %s; loaded via storage-api and cached", tenant_id)
    return resolved


async def get_settings_for_display(tenant_id: str) -> dict:
    """Return ``DEFAULT_SETTINGS`` merged with the tenant's overrides for UI display."""
    raw = await get_raw_settings(tenant_id)
    return _deep_merge(DEFAULT_SETTINGS, raw)


async def update_settings(
    tenant_id: str,
    new_settings: dict,
    *,
    changed_by: str | None = None,
) -> dict:
    """Upsert tenant overrides + write an audit row with the flat diff.

    Returns the merged display view (``DEFAULT_SETTINGS`` ⊕ tenant overrides)
    so callers can echo back the resulting state. No-ops when the submitted
    payload introduces no actual changes.

    The transactional upsert (``FOR UPDATE`` read → flat diff → JSONB ``||``
    merge → audit row, one transaction) runs server-side in core-storage-api
    (Fix 2 Phase 0). Validation, the TTL-cache invalidate, and the
    ``SETTINGS_CHANGED`` broadcast stay here.
    """
    _check_keys(new_settings, DEFAULT_SETTINGS)
    _validate_leaf_types(new_settings)
    _validate_governance_enums(new_settings)
    _validate_default_search_profile(new_settings)
    cron_override = new_settings.get("security_audit", {}).get("schedule_cron")
    if cron_override is not None:
        _validate_cron(cron_override)

    # The diff-against-current + upsert + audit happen in one server-side
    # transaction (the FOR UPDATE lost-update guard can't span an HTTP read +
    # write, so it lives in storage-api). ``merged`` is the resulting raw
    # overrides; ``changed`` is False when the payload was a no-op.
    result = await get_storage_client().update_org_settings(tenant_id, new_settings, changed_by=changed_by)
    merged = result["settings"]
    if not result.get("changed"):
        # Identical payload — storage wrote nothing; nothing to invalidate or broadcast.
        return _deep_merge(DEFAULT_SETTINGS, merged)

    # Invalidate THIS process's cache immediately...
    invalidate_cache(tenant_id)
    # ...and broadcast so every other worker/instance drops its copy promptly
    # too (CAURA-571), instead of serving the stale value for up to the TTL —
    # which matters for a tightened governance control. Best-effort: a publish
    # failure must not fail a settings write that already committed; siblings
    # then fall back to the TTL, exactly as before this change.
    try:
        await get_event_bus().publish(
            Topics.Org.SETTINGS_CHANGED,
            Event(
                event_type=Topics.Org.SETTINGS_CHANGED,
                tenant_id=tenant_id,
                payload=OrgSettingsChangedEvent(org_id=tenant_id).model_dump(mode="json"),
            ),
        )
    except Exception:
        logger.warning(
            "failed to publish settings-changed for %s; sibling workers will "
            "pick up the change within the cache TTL",
            tenant_id,
            exc_info=True,
        )

    return _deep_merge(DEFAULT_SETTINGS, merged)
