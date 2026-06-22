"""Forge cron tick — the production scheduler entry point (SF-CR3).

Mirrors the manual ``memclawctl forge dry-run`` CLI but wired into the
``memclaw.lifecycle.forge-distill-requested`` consumer so the autonomous
scheduler (Cloud Scheduler / k8s CronJob → ``POST /admin/lifecycle/
fanout/forge-distill``) drives one tick per opted-in tenant.

Decomposition:

  1. Resolve per-tenant ``ForgeConfig`` from
     ``org_settings.skills_factory.forge.*``.
  2. Wire injectables (``llm_fn``, ``memory_fetcher``, ``poison_checker``,
     ``candidate_writer``, ``status_checker``) — same shapes as the
     CLI uses, but bound to a request-scoped DB session.
  3. Invoke :func:`run_forge_distill` for the configured freshness
     window.
  4. Invoke :func:`promote_pending_candidates` so newly-minted
     candidates that pass the 6 auto-gates flow to ``staged`` in the
     same tick (no second cron tick needed for promotion).
  5. Return ``candidates_written + promoted`` for the lifecycle_audit
     row's ``stats`` block.

Why one entry point per tenant (rather than a fan-out inside the
handler): the lifecycle fanout endpoint already publishes one event
per tenant (see ``_list_tenants_with_skills_factory_enabled``), and
each event consumes ``run_label`` from the publisher kwargs. Per-tick
isolation gives the audit row + dedup window the granularity to
attribute failures to the right tenant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from core_api.clients.storage_client import get_storage_client
from core_api.services.forge.forge_service import (
    ForgeConfig,
    run_forge_distill,
)
from core_api.services.forge.poison import is_fingerprint_poisoned
from core_api.services.organization_settings import get_settings_for_display
from core_api.services.skill_promoter import (
    make_db_live_data_fetcher,
    make_db_poison_checker,
    make_db_status_updater,
    promote_pending_candidates,
)

logger = logging.getLogger(__name__)


# ── ForgeConfig resolution ────────────────────────────────────────


async def _resolve_forge_config(org_id: str) -> ForgeConfig:
    """Build a per-tenant ``ForgeConfig`` from org_settings overrides.

    Falls through to the dataclass defaults for any unset key — the
    Phase 0 ``DEFAULT_SETTINGS.skills_factory.forge`` block populates
    the same defaults, so the merged view is always concrete.

    ``get_settings_for_display`` is fetched via the storage client (Fix 2
    Phase 0) with a 5-min TTL cache; the ``db`` argument is vestigial there,
    so we pass ``None``.
    """
    settings = await get_settings_for_display(None, org_id)
    sf = (settings or {}).get("skills_factory") or {}
    forge = sf.get("forge") or {}
    # Source fallbacks from a ``ForgeConfig()`` instance rather than
    # hardcoded literals so the dataclass remains the single source
    # of truth — bumping a default in ``forge_service.ForgeConfig``
    # automatically lands here for tenants without explicit overrides.
    # Surfaces ALL configurable knobs (including
    # ``cluster_entity_jaccard_threshold`` and ``memory_excerpt_char_cap``)
    # so a tenant can tune any per-tenant Forge behavior via
    # ``org_settings.skills_factory.forge.*``.
    _d = ForgeConfig()
    return ForgeConfig(
        min_cluster_size=int(forge.get("min_cluster_size", _d.min_cluster_size)),
        min_distinct_agents=int(forge.get("min_distinct_agents", _d.min_distinct_agents)),
        freshness_window_days=int(forge.get("freshness_window_days", _d.freshness_window_days)),
        max_writes_per_run=int(forge.get("max_writes_per_run", _d.max_writes_per_run)),
        body_max_bytes=int(sf.get("body_max_bytes", _d.body_max_bytes)),
        description_max_bytes=int(sf.get("description_max_bytes", _d.description_max_bytes)),
        cluster_entity_jaccard_threshold=float(
            forge.get("cluster_entity_jaccard_threshold", _d.cluster_entity_jaccard_threshold)
        ),
        memory_excerpt_char_cap=int(forge.get("memory_excerpt_char_cap", _d.memory_excerpt_char_cap)),
    )


async def _resolve_auto_promote_clean(org_id: str) -> bool:
    """Read ``skills_factory.sentinel.auto_promote_clean`` for a tenant.

    Default False (HITL preserved). ``get_settings_for_display`` is
    cached (5-min TTL), so the second fetch within a tick — alongside
    ``_resolve_forge_config`` — is a cache hit, not a second round-trip.
    """
    settings = await get_settings_for_display(None, org_id)
    sf = (settings or {}).get("skills_factory") or {}
    sentinel = sf.get("sentinel") or {}
    return bool(sentinel.get("auto_promote_clean", False))


# ── Injectable factories ──────────────────────────────────────────


def _make_memory_fetcher(tenant_id: str):
    """Bulk-load ``memories.content`` by id via core-storage-api.

    Mirrors the dry-run CLI's fetcher. NULL-safe (storage coerces a NULL
    ``content`` → empty string in the response). ``tenant_id`` scopes the
    storage read so the fetch can't cross tenants.
    """
    sc = get_storage_client()

    async def _fetch(memory_ids: list[str]) -> dict[str, str]:
        if not memory_ids:
            return {}
        rows = await sc.forge_memory_content_by_ids(tenant_id=tenant_id, memory_ids=list(memory_ids))
        return {row["id"]: row.get("content") or "" for row in rows}

    return _fetch


def _make_poison_checker():
    """Adapt :func:`is_fingerprint_poisoned` (storage-backed) to the
    ``(tenant, fleet, fp) → bool`` shape the gate evaluator expects.
    """

    async def _check(tenant_id: str, fleet_id: str | None, fingerprint: str) -> bool:
        return await is_fingerprint_poisoned(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            cluster_fingerprint=fingerprint,
        )

    return _check


def _make_candidate_writer():
    """Persist a fresh Forge candidate via the storage HTTP client.

    Uses ``upsert_document`` so re-running the same cluster (same
    fingerprint → same slug) overwrites a prior candidate idempotently.
    """
    sc = get_storage_client()

    async def _write(candidate_doc: dict[str, Any]) -> None:
        await sc.upsert_document(candidate_doc)

    return _write


def _make_status_checker():
    """Existence check used to skip writes against already-active /
    rejected / quarantined docs. Returns the live ``data.status`` or
    ``None`` if the slug doesn't exist yet.
    """
    sc = get_storage_client()

    async def _check(tenant_id: str, collection: str, doc_id: str) -> str | None:
        doc = await sc.get_document(tenant_id=tenant_id, collection=collection, doc_id=doc_id)
        if doc is None:
            return None
        data = doc.get("data") if isinstance(doc, dict) else None
        return (data or {}).get("status") if isinstance(data, dict) else None

    return _check


async def _wire_llm_fn():
    """Resolve a working LLM callable from the project's existing
    provider plumbing. Falls back to a structured ``RuntimeError`` if
    the provider chain isn't importable — the cron should NEVER
    silently substitute a fake LLM in production.
    """
    # Lazy import — ``common.llm`` pulls in vertex/openai SDKs that
    # we don't want loading at core-api startup just for the cron
    # adapter wiring.
    try:
        from common.llm import (  # type: ignore[import-not-found]
            LLMRequest,
            call_with_fallback,
        )
    except ImportError as exc:
        raise RuntimeError(
            "forge_cron: common.llm not importable — the production "
            "cron requires a configured LLM provider chain. The fake-"
            "LLM fallback is intentionally CLI-only (memclawctl forge "
            "dry-run); see scripts/forge_dry_run.py."
        ) from exc

    async def _llm_fn(prompt: str) -> str:
        request = LLMRequest(messages=[{"role": "user", "content": prompt}])
        response = await call_with_fallback(request, expecting="json")
        return response.text

    return _llm_fn


# ── Public entry point ────────────────────────────────────────────


async def run_forge_cron_tick(
    *,
    tenant_id: str,
    fleet_id: str | None,
    run_label: str,
) -> dict[str, int]:
    """One cron tick: mine fresh candidates + promote any that pass
    the auto-gates.

    As of Fix 2 Ph5a this opens no DB session — every read/write the tick
    needs (forge poison, session traces, outcome signals, candidate scan +
    CAS status flip) goes through core-storage-api via the storage client.
    ``tenant_id`` is threaded everywhere a session used to be.

    Returns a dict the lifecycle_audit row stores under ``stats``:
      * ``candidates_written`` — number of fresh candidates produced
        in this tick (matches ``ForgeRunResult.candidates_written``).
      * ``promoted`` — number of candidates that flowed
        ``candidate → staged`` in the same tick.
      * ``scanned``, ``held``, plus the 5 Forge skip counters — so an
        operator inspecting an audit row can see exactly what the
        tick did without running the dry-run CLI to reproduce.

    Exceptions propagate so the shared lifecycle handler marks the
    audit row ``failure`` with the exception text; the next tick
    retries on its normal schedule.
    """
    cfg = await _resolve_forge_config(tenant_id)
    auto_promote_clean = await _resolve_auto_promote_clean(tenant_id)
    now = datetime.now(UTC)
    window_end = now
    window_start = now - timedelta(days=cfg.freshness_window_days)

    llm_fn = await _wire_llm_fn()
    memory_fetcher = _make_memory_fetcher(tenant_id)
    poison_checker = _make_poison_checker()
    candidate_writer = _make_candidate_writer()
    status_checker = _make_status_checker()

    # ``run_forge_distill`` keeps a (now-vestigial) first positional arg for
    # CLI / test-call-site compatibility; it no longer touches the DB —
    # ``build_session_traces`` + the injected fetchers route through storage.
    forge_result = await run_forge_distill(
        None,
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        window_start=window_start,
        window_end=window_end,
        run_label=run_label,
        llm_fn=llm_fn,
        memory_fetcher=memory_fetcher,
        poison_checker=poison_checker,
        candidate_writer=candidate_writer,
        status_checker=status_checker,
        config=cfg,
    )

    # Same-tick promotion: candidates whose 6 auto-gates pass land in
    # ``staged`` without waiting for a second cron firing. Failures
    # (poison hit, scan dirty, hash-binding stale) are held — they
    # surface on the next tick if conditions change.
    promote_result = await promote_pending_candidates(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        poison_checker=make_db_poison_checker(),
        live_data_fetcher=make_db_live_data_fetcher(),
        status_updater=make_db_status_updater(expected_status="candidate"),
        min_cluster_size=cfg.min_cluster_size,
        min_distinct_agents=cfg.min_distinct_agents,
        freshness_window_days=cfg.freshness_window_days,
        now=now,
        auto_promote_clean=auto_promote_clean,
    )

    stats = {
        "candidates_written": forge_result.candidates_written,
        "promoted": promote_result.promoted,
        # Subset of ``promoted`` that skipped the Inbox and went
        # straight to ``active`` (opt-in ``auto_promote_clean`` +
        # clean Sentinel scan). ``promoted - auto_approved`` is the
        # count that landed in ``staged`` for human review.
        "auto_approved": promote_result.auto_approved,
        "scanned": promote_result.scanned,
        "held": promote_result.held,
        # Surface the 5 Forge skip buckets so audit rows are
        # actionable — "3 io_errors" vs "1 sentinel block" vs
        # "5 poisoned" tells an operator very different stories.
        "skipped_poisoned": forge_result.candidates_skipped_poisoned,
        "skipped_sentinel": forge_result.candidates_skipped_sentinel,
        "skipped_distill_error": forge_result.candidates_skipped_distill_error,
        "skipped_io_error": forge_result.candidates_skipped_io_error,
        "skipped_existing": forge_result.candidates_skipped_existing,
    }
    logger.info(
        "forge cron tick: tenant=%s fleet=%s window=[%s,%s] %s",
        tenant_id,
        fleet_id,
        window_start.isoformat(),
        window_end.isoformat(),
        stats,
    )
    return stats
