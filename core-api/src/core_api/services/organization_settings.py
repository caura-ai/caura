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
from typing import Any

from cachetools import TTLCache
from croniter import CroniterBadCronError, croniter
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.organization_settings import OrganizationSettings, OrganizationSettingsAudit
from common.provider_names import ProviderName
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
    "search": {
        "recall_boost": None,
        "graph_retrieval": None,
    },
    "crystallizer": {
        "auto_crystallize": None,
    },
    "dedup": {
        "semantic_dedup_enabled": None,
    },
    "lifecycle": {
        "lifecycle_automation_enabled": None,
    },
    "entity_linking": {
        "auto_entity_linking_enabled": None,
    },
    "chunking": {
        "auto_chunk_enabled": None,
    },
    "write": {
        "default_write_mode": None,  # None = "fast"; "fast" | "strong"
    },
    "agents": {
        "require_agent_approval": None,
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


def _deep_merge(old: Any, new: Any) -> Any:
    """Return ``old`` with ``new`` merged recursively for nested dicts.

    Non-dict values in ``new`` overwrite ``old`` wholesale (including lists).
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _diff_settings(old: dict, new: dict, prefix: str = "") -> dict:
    """Flat diff: ``{"enrichment.provider": [old, new], ...}``.

    Recurses into nested dicts; treats non-dict values as leaves. Only records
    keys present in ``new`` whose value differs from ``old``; does not record
    deletions (updates are additive).
    """
    out: dict = {}
    for k, new_v in new.items():
        path = f"{prefix}{k}"
        old_v = old.get(k) if isinstance(old, dict) else None
        if isinstance(new_v, dict):
            # Recurse even when the old side is absent, so we always emit flat
            # leaf keys (e.g. "security_audit.schedule_enabled") rather than a
            # whole-dict diff ["enrichment": [None, {...}]].
            old_dict = old_v if isinstance(old_v, dict) else {}
            out.update(_diff_settings(old_dict, new_v, prefix=f"{path}."))
        elif new_v != old_v:
            out[path] = [old_v, new_v]
    return out


def _validate_cron(expr: str) -> None:
    """Raise ``ValueError`` if *expr* is not a valid cron expression."""
    try:
        croniter(expr)
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression {expr!r}: {exc}") from exc


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
    "entity_linking.auto_entity_linking_enabled": bool,
    "chunking.auto_chunk_enabled": bool,
    "agents.require_agent_approval": bool,
    "entity_blocklist": list,
}


def _validate_leaf_types(payload: dict, prefix: str = "") -> None:
    """Raise ``ValueError`` if any leaf value has the wrong Python type."""
    for k, v in payload.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            _validate_leaf_types(v, prefix=f"{path}.")
        elif v is not None and path in _LEAF_TYPES:
            expected = _LEAF_TYPES[path]
            if not isinstance(v, expected):
                type_name = (
                    expected.__name__
                    if isinstance(expected, type)
                    else " or ".join(t.__name__ for t in expected)
                )
                raise ValueError(f"Settings key {path!r} must be {type_name}, got {type(v).__name__}")


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

    # Entity linking
    @property
    def auto_entity_linking_enabled(self) -> bool:
        val = self._ts.get("entity_linking", {}).get("auto_entity_linking_enabled")
        return val if val is not None else True

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
            return frozenset(custom)
        return frozenset(DEFAULT_SETTINGS["entity_blocklist"])

    # Write mode
    @property
    def default_write_mode(self) -> str:
        val = self._ts.get("write", {}).get("default_write_mode")
        if val in ("fast", "strong"):
            return val
        return "fast"  # default to fast when unset

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


async def resolve_config(db: AsyncSession | None, tenant_id: str) -> ResolvedConfig:
    """Resolve config for a tenant: tenant override → global env default.

    ``db`` may be ``None`` for fire-and-forget callers (post-commit
    contradiction detection, the CAURA-595 ENRICHED consumer) — see
    :func:`get_raw_settings` for the cold-cache fallback.
    """
    raw = await get_raw_settings(db, tenant_id)
    return ResolvedConfig(raw)


async def get_raw_settings(db: AsyncSession | None, tenant_id: str) -> dict:
    """Return the tenant's raw override dict, or ``{}`` if no overrides set.

    Cache-first: returns ``{}`` for tenants that have never been configured.

    ``db is None`` is the fire-and-forget path: post-commit detection
    (the request session has closed), the CAURA-595 ``ENRICHED``
    consumer (Pub/Sub handler with no ambient request session), and
    similar callers can pass ``None`` and rely on the cache. On a
    cache miss with ``db is None`` we open a fresh session here
    rather than crash with ``AttributeError: 'NoneType' object has
    no attribute 'execute'`` — which is what happened in production
    until this fallback landed (CAURA-595 Phase 5a brought the
    consumer up; cold-start always missed the cache; every event
    crashed inside detection before this guard).
    """
    cached = _settings_cache.get(tenant_id)
    if cached is not None:
        logger.debug("organization_settings cache hit for %s", tenant_id)
        return cached

    if db is None:
        # Lazy import — db.session imports SQLAlchemy engine which
        # touches DATABASE_URL at module-import time; keeping this
        # behind a cache miss means the standalone OSS path that
        # never hits the cold-cache branch doesn't pay the cost.
        from core_api.db.session import async_session

        async with async_session() as session:
            return await _load_and_cache(session, tenant_id)

    return await _load_and_cache(db, tenant_id)


async def _load_and_cache(db: AsyncSession, tenant_id: str) -> dict:
    result = await db.execute(
        select(OrganizationSettings.settings).where(OrganizationSettings.org_id == tenant_id)
    )
    row = result.scalar_one_or_none()
    resolved = row if isinstance(row, dict) else {}
    _settings_cache[tenant_id] = resolved
    logger.info("organization_settings cache miss for %s; loaded from DB and cached", tenant_id)
    return resolved


async def get_settings_for_display(db: AsyncSession | None, tenant_id: str) -> dict:
    """Return ``DEFAULT_SETTINGS`` merged with the tenant's overrides for UI display."""
    raw = await get_raw_settings(db, tenant_id)
    return _deep_merge(DEFAULT_SETTINGS, raw)


async def update_settings(
    db: AsyncSession,
    tenant_id: str,
    new_settings: dict,
    *,
    changed_by: str | None = None,
) -> dict:
    """Upsert tenant overrides + write an audit row with the flat diff.

    Returns the merged display view (``DEFAULT_SETTINGS`` ⊕ tenant overrides)
    so callers can echo back the resulting state. No-ops when the submitted
    payload introduces no actual changes.
    """
    _check_keys(new_settings, DEFAULT_SETTINGS)
    _validate_leaf_types(new_settings)
    cron_override = new_settings.get("security_audit", {}).get("schedule_cron")
    if cron_override is not None:
        _validate_cron(cron_override)

    # Read current overrides straight from DB — we don't want to diff against a
    # stale cache entry, and this path is rare compared to reads.
    # FOR UPDATE prevents lost-update races under concurrent writes for the same tenant.
    result = await db.execute(
        select(OrganizationSettings.settings)
        .where(OrganizationSettings.org_id == tenant_id)
        .with_for_update()
    )
    current_row = result.scalar_one_or_none()
    current: dict = current_row if isinstance(current_row, dict) else {}

    diff = _diff_settings(current, new_settings)
    if not diff:
        # Identical payload — skip write and audit row entirely.
        return _deep_merge(DEFAULT_SETTINGS, current)

    merged = _deep_merge(current, new_settings)

    # FOR UPDATE serialises all writes once the row exists. Concurrent first-time
    # inserts (no row yet) use JSONB || to merge at the DB level so two racing
    # inserts don't silently overwrite each other. The shallow || merge is safe
    # because top-level schema keys (enrichment, recall, …) are independent.
    upsert = pg_insert(OrganizationSettings).values(org_id=tenant_id, settings=merged)
    await db.execute(
        upsert.on_conflict_do_update(
            index_elements=["org_id"],
            set_={
                "settings": text("organization_settings.settings || EXCLUDED.settings"),
                "updated_at": func.now(),
            },
        )
    )
    await db.execute(
        pg_insert(OrganizationSettingsAudit).values(org_id=tenant_id, changed_by=changed_by, diff=diff)
    )
    await db.commit()

    # Local invalidation — other workers pick up the change within TTL.
    invalidate_cache(tenant_id)

    return _deep_merge(DEFAULT_SETTINGS, merged)
