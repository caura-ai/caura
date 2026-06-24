"""Storage, cache, and ResolvedConfig tests for organization_settings service."""

import uuid

import pytest
from sqlalchemy import text

from common.organization_settings_merge import deep_merge as _deep_merge
from common.organization_settings_merge import diff_settings as _diff_settings
from core_api.clients.storage_client import get_storage_client
from core_api.services import organization_settings as ts_svc
from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    ResolvedConfig,
    get_raw_settings,
    get_settings_for_display,
    invalidate_cache,
    resolve_config,
    update_settings,
)


def _tid() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the module-level TTLCache between tests so state doesn't leak."""
    ts_svc._settings_cache.clear()
    yield
    ts_svc._settings_cache.clear()


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_deep_merge_nested_dicts():
    old = {"enrichment": {"provider": "openai", "model": "gpt-4"}}
    new = {"enrichment": {"provider": "vertex"}}
    assert _deep_merge(old, new) == {
        "enrichment": {"provider": "vertex", "model": "gpt-4"}
    }


def test_deep_merge_overwrites_lists():
    old = {"entity_blocklist": ["team", "project"]}
    new = {"entity_blocklist": ["custom"]}
    assert _deep_merge(old, new) == {"entity_blocklist": ["custom"]}


def test_diff_settings_flat_keys():
    old = {"enrichment": {"provider": "openai", "model": "gpt-4"}}
    new = {"enrichment": {"provider": "vertex", "model": "gpt-4"}}
    assert _diff_settings(old, new) == {
        "enrichment.provider": ["openai", "vertex"],
    }


def test_diff_settings_empty_when_identical():
    old = {"search": {"recall_boost": True}}
    new = {"search": {"recall_boost": True}}
    assert _diff_settings(old, new) == {}


def test_diff_settings_new_keys():
    old: dict = {}
    new = {"security_audit": {"schedule_enabled": True}}
    assert _diff_settings(old, new) == {
        "security_audit.schedule_enabled": [None, True],
    }


# ── ResolvedConfig ────────────────────────────────────────────────────────


def test_resolved_config_empty_uses_global_defaults():
    cfg = ResolvedConfig({})
    assert cfg.recall_boost is True
    assert cfg.auto_crystallize_enabled is True
    assert cfg.semantic_dedup_enabled is True
    assert cfg.lifecycle_automation_enabled is True
    assert cfg.auto_entity_linking_enabled is True
    assert cfg.auto_chunk_enabled is False  # opt-in
    assert cfg.require_agent_approval is False
    assert cfg.default_write_mode == "fast"


def test_resolved_config_security_audit_defaults_opt_in():
    """security_audit schedule + alerts default to False (opt-in)."""
    cfg = ResolvedConfig({})
    assert cfg.security_audit_schedule_enabled is False
    assert cfg.security_audit_alerts_enabled is False
    assert cfg.security_audit_schedule_cron == "0 2 * * *"
    assert cfg.security_audit_alert_recipients == []
    assert cfg.security_audit_alert_score_below is None
    assert cfg.security_audit_alert_critical_findings_min is None
    assert cfg.security_audit_alert_score_drop_delta is None


def test_resolved_config_tenant_override_wins():
    cfg = ResolvedConfig(
        {
            "security_audit": {
                "schedule_enabled": True,
                "schedule_cron": "0 3 * * 1",
                "alert_recipients": ["ops@example.com"],
                "alert_score_below": 75.0,
            }
        }
    )
    assert cfg.security_audit_schedule_enabled is True
    assert cfg.security_audit_schedule_cron == "0 3 * * 1"
    assert cfg.security_audit_alert_recipients == ["ops@example.com"]
    assert cfg.security_audit_alert_score_below == 75.0


# ── Storage: get_raw_settings / update_settings ───────────────────────────


async def test_get_empty_returns_empty_dict(db):
    raw = await get_raw_settings(_tid())
    assert raw == {}


async def test_get_display_empty_returns_schema_with_nulls(db):
    display = await get_settings_for_display(_tid())
    # Every schema key from DEFAULT_SETTINGS should be present
    for key in DEFAULT_SETTINGS:
        assert key in display
    assert display["security_audit"]["schedule_enabled"] is None
    assert display["enrichment"]["provider"] is None


async def test_update_persists_overrides(db):
    tid = _tid()
    await update_settings(tid,
        {"enrichment": {"provider": "vertex", "model": "gemini-2.0-flash"}},
    )

    raw = await get_raw_settings(tid)
    assert raw["enrichment"]["provider"] == "vertex"
    assert raw["enrichment"]["model"] == "gemini-2.0-flash"


async def test_update_merges_nested_keys(db):
    """Partial update of one nested key doesn't wipe sibling keys in the same block."""
    tid = _tid()
    await update_settings(tid, {"enrichment": {"provider": "openai", "model": "gpt-4"}}
    )
    invalidate_cache(tid)
    await update_settings(tid, {"enrichment": {"provider": "vertex"}})

    raw = await get_raw_settings(tid)
    assert raw["enrichment"]["provider"] == "vertex"
    assert raw["enrichment"]["model"] == "gpt-4"  # preserved


async def test_update_partial_preserves_other_features(db):
    """Updating security_audit doesn't clear a previously-set enrichment override."""
    tid = _tid()
    await update_settings(tid, {"enrichment": {"provider": "openai"}})
    invalidate_cache(tid)
    await update_settings(tid, {"security_audit": {"schedule_enabled": True}})

    raw = await get_raw_settings(tid)
    assert raw["enrichment"]["provider"] == "openai"
    assert raw["security_audit"]["schedule_enabled"] is True


async def test_audit_row_written_on_change(db):
    tid = _tid()
    await update_settings(tid,
        {"enrichment": {"provider": "vertex"}},
        changed_by="user-123",
    )

    result = await db.execute(
        text(
            "SELECT changed_by, diff FROM organization_settings_audit "
            "WHERE org_id = :tid ORDER BY created_at DESC LIMIT 1"
        ),
        {"tid": tid},
    )
    row = result.first()
    assert row is not None
    assert row.changed_by == "user-123"
    assert row.diff == {"enrichment.provider": [None, "vertex"]}


async def test_audit_no_row_on_noop(db):
    """PUT with identical payload to the current state writes neither row."""
    tid = _tid()
    await update_settings(tid, {"enrichment": {"provider": "vertex"}})

    before = await db.execute(
        text("SELECT count(*) FROM organization_settings_audit WHERE org_id = :tid"),
        {"tid": tid},
    )
    before_count = before.scalar() or 0

    invalidate_cache(tid)
    await update_settings(tid, {"enrichment": {"provider": "vertex"}})

    after = await db.execute(
        text("SELECT count(*) FROM organization_settings_audit WHERE org_id = :tid"),
        {"tid": tid},
    )
    after_count = after.scalar() or 0
    assert after_count == before_count


async def test_resolve_config_reads_from_db(db):
    tid = _tid()
    await update_settings(tid, {"security_audit": {"schedule_enabled": True}})
    invalidate_cache(tid)

    cfg = await resolve_config(tid)
    assert cfg.security_audit_schedule_enabled is True


# ── Cache semantics ───────────────────────────────────────────────────────


async def test_cache_hit_avoids_storage_fetch(db, monkeypatch):
    """Second call to get_raw_settings for the same tenant should not re-fetch
    from core-storage-api — the TTL cache short-circuits before the HTTP call."""
    tid = _tid()
    await get_raw_settings(tid)  # populates cache with {} (one storage fetch)

    calls = {"n": 0}
    sc = get_storage_client()
    original_get = sc.get_org_settings

    async def counting_get(org_id):
        calls["n"] += 1
        return await original_get(org_id)

    monkeypatch.setattr(sc, "get_org_settings", counting_get)

    await get_raw_settings(tid)
    await get_raw_settings(tid)
    assert calls["n"] == 0, "Cache hits should not fetch from storage"


async def test_update_invalidates_local_cache(db):
    """After update_settings, the next read returns the new value without waiting for TTL."""
    tid = _tid()
    # Prime cache with empty
    assert await get_raw_settings(tid) == {}

    await update_settings(tid, {"security_audit": {"alerts_enabled": True}})

    raw = await get_raw_settings(tid)
    assert raw["security_audit"]["alerts_enabled"] is True


# ── db=None fire-and-forget callers (CAURA-595 Phase 5a; Fix 2 Phase 0) ───
#
# After Fix 2 Phase 0 the ``db`` argument is vestigial — settings load via the
# storage client, so the old "open a self-session on db=None" fallback is gone.
# These pin that fire-and-forget callers (the ENRICHED consumer, post-commit
# detection) still resolve settings with ``db=None`` and that a warm cache
# short-circuits the storage fetch entirely.


async def test_get_raw_settings_works_when_db_is_none(db):
    """Cold-cache call with ``db=None`` must resolve via the storage client
    (no DB session needed) and land the result in the cache for next time."""
    tid = _tid()
    # Seed an override row (via the storage-client write path) so we can prove
    # the db=None read actually fetched and cached it.
    await update_settings(tid, {"enrichment": {"provider": "openai"}})
    ts_svc._settings_cache.clear()  # discard the warm cache from update_settings

    raw = await get_raw_settings(tid)

    assert raw["enrichment"]["provider"] == "openai"
    # Cache populated by the storage fetch — next call is a cache hit.
    assert tid in ts_svc._settings_cache


async def test_resolve_config_works_without_db_session(db):
    """End-to-end: ``resolve_config(tenant_id)`` (the path the
    fire-and-forget contradiction detector + the CAURA-595 Phase 5a
    consumer take) must return a usable ``ResolvedConfig`` without a
    request-scoped session in scope."""
    tid = _tid()
    await update_settings(tid, {"enrichment": {"provider": "openai"}})
    ts_svc._settings_cache.clear()

    config = await resolve_config(tid)

    assert config.enrichment_provider == "openai"


async def test_get_raw_settings_skips_storage_fetch_on_cache_hit(monkeypatch):
    """``db=None`` with the cache already warm must NOT hit storage — fetching
    per detection event would be a hot-path regression. The cache hit should
    short-circuit before the storage client is even called."""
    tid = _tid()
    ts_svc._settings_cache[tid] = {"enrichment": {"provider": "anthropic"}}

    async def _fail_fetch(_org_id):
        raise AssertionError("storage must not be fetched on a cache hit")

    monkeypatch.setattr(get_storage_client(), "get_org_settings", _fail_fetch)

    raw = await get_raw_settings(tid)
    assert raw == {"enrichment": {"provider": "anthropic"}}


# ── Storage-API POST input validation (claude-review on #427) ─────────────
#
# These hit the core-storage-api router directly (via the storage_http bridge)
# with malformed bodies the typed client would never send. Both guards reject
# BEFORE any DB access.


def _settings_path(org_id: str) -> str:
    return f"/api/v1/storage/organization-settings/{org_id}"


async def test_post_non_object_body_returns_422(storage_http):
    """A valid-JSON-but-non-object body (list/scalar) must 422, not 500 — the
    pre-guard fix for the AttributeError on ``body.get(...)``."""
    resp = await storage_http.post(_settings_path(_tid()), json=["not", "an", "object"])
    assert resp.status_code == 422, resp.text


async def test_post_non_string_changed_by_returns_422(storage_http):
    """``changed_by`` must be a string or null; a non-string returns a clean
    422 rather than a confusing DB type error on the Text column."""
    resp = await storage_http.post(
        _settings_path(_tid()), json={"settings": {}, "changed_by": 123}
    )
    assert resp.status_code == 422, resp.text
