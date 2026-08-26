"""Forge cron tick — the production scheduler entry point (SF-CR3).

Mirrors the manual ``scripts/forge_dry_run.py`` harness but wired into the
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
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from common.events.base import PermanentOpError
from core_api.clients.storage_client import get_storage_client
from core_api.services.forge.forge_service import (
    CandidateWriter,
    ForgeConfig,
    LlmFn,
    MemoryFetcher,
    PoisonChecker,
    StatusChecker,
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
    settings = await get_settings_for_display(org_id)
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
    settings = await get_settings_for_display(org_id)
    sf = (settings or {}).get("skills_factory") or {}
    sentinel = sf.get("sentinel") or {}
    return bool(sentinel.get("auto_promote_clean", False))


# ── Injectable factories ──────────────────────────────────────────


def _make_memory_fetcher(tenant_id: str) -> MemoryFetcher:
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


def _make_poison_checker(tenant_id: str, fleet_id: str | None) -> PoisonChecker:
    """Adapt :func:`is_fingerprint_poisoned` (storage-backed) to the
    ``PoisonChecker`` seam ``run_forge_distill`` actually calls:
    ``(fingerprint) → bool``, with the tenant and fleet closed over.

    H-08: this used to take ``(tenant_id, fleet_id, fingerprint)`` — the shape
    ``evaluate_auto_gates`` wants — while its only caller feeds
    ``run_forge_distill``, which invokes ``await poison_checker(fingerprint.fp)``
    with one argument. Every cluster therefore raised ``TypeError`` after the LLM
    distill call, ``run_forge_distill``'s broad ``except Exception`` counted it as
    ``skipped_io_error``, and the tick reported success having written nothing.

    The gate-evaluator shape was never needed here: ``promote_pending_candidates``
    is handed :func:`make_db_poison_checker` (skill_promoter), which is the
    three-arg wrapper for that path. Closing over the identifiers instead mirrors
    ``scripts/forge_dry_run.py``'s ``_wire_poison_checker``, which is why the
    dry-run CLI was never affected.
    """

    async def _check(fingerprint: str) -> bool:
        return await is_fingerprint_poisoned(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            cluster_fingerprint=fingerprint,
        )

    return _check


def _make_candidate_writer() -> CandidateWriter:
    """Persist a fresh Forge candidate via the storage HTTP client.

    Uses ``upsert_document`` so re-running the same cluster (same
    fingerprint → same slug) overwrites a prior candidate idempotently.
    """
    sc = get_storage_client()

    async def _write(candidate_doc: dict[str, Any]) -> None:
        await sc.upsert_document(candidate_doc)

    return _write


def _make_status_checker() -> StatusChecker:
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


async def _wire_llm_fn() -> LlmFn:
    """Resolve a working LLM callable from the project's existing
    provider plumbing. Falls back to a structured ``RuntimeError`` if
    the provider chain isn't importable — the cron should NEVER
    silently substitute a fake LLM in production.
    """
    # Lazy import — ``common.llm`` pulls in vertex/openai SDKs that
    # we don't want loading at core-api startup just for the cron
    # adapter wiring.
    #
    # Every name here is one ``common.llm`` actually exports. It used to ask for
    # ``LLMRequest``, which has never existed in that module, so the import below
    # always raised and the ``except`` clause below always ran — meaning this
    # function raised RuntimeError on every tick and the distill cron could not
    # run at all. Worse, the message blamed a missing provider chain, so the
    # symptom read as a deployment problem rather than as a wrong import.
    try:
        from common.llm import LLMProvider, call_with_fallback
        from common.llm.retry import deliberate_fake_provider
        from common.provider_names import ProviderName
    except ImportError as exc:
        raise RuntimeError(
            "forge_cron: common.llm not importable — the production "
            "cron requires a configured LLM provider chain. The fake-"
            "LLM fallback is intentionally CLI-only; see "
            "scripts/forge_dry_run.py."
        ) from exc

    # Same selector the enrichment write path uses. Forge has no provider
    # setting of its own, and ``call_with_fallback`` resolves per-tenant model
    # overrides against ``enrichment_model`` by default, so borrowing the
    # enrichment provider keeps the two halves of one write pipeline on the same
    # provider. A dedicated ``FORGE_PROVIDER`` would be a new config surface and
    # belongs in its own change, not in a repair.
    provider_name = os.environ.get("ENTITY_EXTRACTION_PROVIDER", ProviderName.OPENAI)

    def _refuse_fake() -> str:
        """``call_with_fallback``'s last resort, which this caller must not take.

        This tick PERSISTS what the LLM returns, as skill candidates that
        promote into ``staged``. ``deliberate_fake_provider`` documents the line
        that matters for persisting callers: a configured ``fake`` provider is an
        operator asking for a stub, while a real provider whose every attempt
        failed is an outage — and an outage is not a request for made-up output.
        Neither case may write placeholder skills here, so both raise; the
        distinction is kept in the message because the two want different
        operator responses. Same posture as ``contradiction_detector``, which
        abstains rather than guessing a verdict.
        """
        if deliberate_fake_provider(provider_name):
            raise RuntimeError(
                "forge_cron: ENTITY_EXTRACTION_PROVIDER is 'fake'. The fake-LLM "
                "fallback is intentionally CLI-only (see scripts/forge_dry_run.py) "
                "— the cron will not mint placeholder skill candidates."
            )
        raise RuntimeError(
            "forge_cron: every configured LLM provider failed for "
            f"'{provider_name}'. Refusing to persist placeholder skill candidates."
        )

    async def _llm_fn(prompt: str) -> str:
        async def _call(llm: LLMProvider) -> str:
            # ``complete_text``, not ``complete_json``: ``LlmFn``'s contract is
            # raw text, which ``parse_distill_response`` parses downstream.
            return await llm.complete_text(prompt)

        return await call_with_fallback(
            primary_provider_name=provider_name,
            call_fn=_call,
            fake_fn=_refuse_fake,
            service_label="forge_cron",
        )

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
      * ``scanned``, ``held``, plus the 6 Forge skip counters — so an
        operator inspecting an audit row can see exactly what the
        tick did without running the dry-run CLI to reproduce.

    Exceptions propagate so the shared lifecycle handler marks the
    audit row ``failure`` with the exception text; the next tick
    retries on its normal schedule.

    Raises :class:`PermanentOpError` when the mining half wrote nothing
    and hit programming errors — a deterministic wiring failure, which
    the runner records as ``failure`` WITHOUT redelivering. Anything
    else raised here is treated as retryable in the usual way.
    """
    cfg = await _resolve_forge_config(tenant_id)
    auto_promote_clean = await _resolve_auto_promote_clean(tenant_id)
    now = datetime.now(UTC)
    window_end = now
    window_start = now - timedelta(days=cfg.freshness_window_days)

    llm_fn = await _wire_llm_fn()
    memory_fetcher = _make_memory_fetcher(tenant_id)
    poison_checker = _make_poison_checker(tenant_id, fleet_id)
    candidate_writer = _make_candidate_writer()
    status_checker = _make_status_checker()

    # ``run_forge_distill`` is keyword-only — it takes NO positional arg. It no
    # longer touches the DB either; ``build_session_traces`` + the injected
    # fetchers route through storage. This comment used to say the vestigial
    # first positional was kept "for CLI / test-call-site compatibility", which
    # stopped being true when the parameter was removed, and the CLI kept
    # passing a session positionally on the strength of it.
    forge_result = await run_forge_distill(
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
        # Surface the 6 Forge skip buckets so the structured log line below is
        # actionable — "3 io_errors" vs "1 sentinel block" vs "5 poisoned" tells
        # an operator very different stories.
        #
        # These reach the LOG, not the audit row: ``lifecycle_audit`` reduces this
        # whole dict to ``candidates_written + promoted`` and stores that single
        # int as ``stats.candidates_produced``. So a log-based alert can key on
        # these; an audit-row query cannot. (The previous wording claimed audit
        # rows, for five buckets — it was wrong then too.)
        "skipped_poisoned": forge_result.candidates_skipped_poisoned,
        "skipped_sentinel": forge_result.candidates_skipped_sentinel,
        "skipped_distill_error": forge_result.candidates_skipped_distill_error,
        "skipped_io_error": forge_result.candidates_skipped_io_error,
        # Non-zero here means a bug in our code, not a bad day for storage — the
        # distinction H-08 (#818) did not have. Unlike every other bucket, a
        # healthy deployment never produces this one, which makes it the one worth
        # alerting on.
        "skipped_internal_error": forge_result.candidates_skipped_internal_error,
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

    # A mining half that wrote nothing AND hit programming errors is a broken
    # deployment, not a quiet day — so the tick must not report success. Permanent
    # rather than a generic raise because the failure is deterministic; see
    # ``PermanentOpError``.
    #
    # Raised AFTER the log above, deliberately: the audit row carries only
    # ``candidates_written + promoted``, and this path forfeits even that, so the
    # log line is the sole record of what actually happened.
    #
    # The condition is about the MINING half only. ``promote_result.promoted`` is
    # not consulted, so a tick that promoted earlier candidates and then failed to
    # mine still fails — that promotion work survives in the log line above but not
    # in the audit row. Deliberate: a wiring bug that silences mining is the more
    # important signal, and it is what went unnoticed for months. What the guard
    # does protect is the common case — ``candidates_written`` non-zero means one
    # malformed cluster among successes, which is routine and must not fail a tick
    # or page anyone.
    if forge_result.candidates_skipped_internal_error and not forge_result.candidates_written:
        raise PermanentOpError(
            f"forge tick wrote no candidates and hit "
            f"{forge_result.candidates_skipped_internal_error} programming error(s) "
            f"across {forge_result.clusters_eligible} eligible cluster(s) "
            f"(tenant={tenant_id} fleet={fleet_id} run={run_label}) — a code/wiring "
            f"bug; see candidates_skipped_internal_error and the tracebacks above"
        )

    return stats
