"""SF-CR4 — Forge cron-tick wiring tests.

Three behaviors verified hermetically (no DB, no LLM, no Pub/Sub):

  1. The lifecycle fanout's tenant discovery filters to opted-in
     tenants ONLY when ``action='forge-distill'``.
  2. ``resolve_publisher_kwargs('forge-distill', org_id)`` stamps a
     deterministic ``run_label``.
  3. ``run_forge_cron_tick`` wires ``run_forge_distill`` +
     ``promote_pending_candidates`` correctly and returns a stats
     dict suitable for the audit row.

The Forge service + promoter themselves are exercised by their own
test files (test_forge_distill.py, test_skill_lifecycle_transitions.py)
with hermetic injected callables; this file just confirms the cron
adapter wires them up correctly.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from core_api.services.forge.cron_handler import _resolve_forge_config
from core_api.services.forge.forge_service import ForgeConfig, ForgeRunResult
from core_api.services.lifecycle_audit import resolve_publisher_kwargs
from core_api.services.skill_promoter import PromoterRunResult


# ── Tenant discovery filter ───────────────────────────────────────


@pytest.mark.unit
class TestTenantFilter:
    """The forge fanout MUST filter to opted-in tenants. A non-opted-in
    tenant is invisible to the cron — no message published, no audit
    row written.

    The actual SQL is exercised by core-storage's integration tests;
    here we just confirm the route dispatcher selects the right helper
    for ``action='forge-distill'``.
    """

    @pytest.mark.asyncio
    async def test_forge_distill_routes_to_opted_in_helper(self):
        from core_api.routes.lifecycle import _list_tenants_for_action

        with patch(
            "core_api.routes.lifecycle.list_tenants_with_skills_factory_enabled",
            new=AsyncMock(return_value=["tenant-a", "tenant-c"]),
        ) as opted_in:
            with patch(
                "core_api.routes.lifecycle.list_active_tenant_ids",
                new=AsyncMock(return_value=["all-other-tenants"]),
            ) as active_all:
                result = await _list_tenants_for_action("forge-distill")
        assert result == ["tenant-a", "tenant-c"]
        opted_in.assert_awaited_once()
        # Critical invariant: the broad "active tenants" helper MUST NOT
        # be called for forge-distill — that'd defeat the opt-in gate
        # and tenants who never enabled the flag would still receive a
        # published event.
        active_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_actions_still_use_active_tenants_helper(self):
        """Regression: the forge-distill branch must not steal the
        default path for archive / insights / crystallize."""
        from core_api.routes.lifecycle import _list_tenants_for_action

        with patch(
            "core_api.routes.lifecycle.list_active_tenant_ids",
            new=AsyncMock(return_value=["a", "b"]),
        ) as active_all:
            with patch(
                "core_api.routes.lifecycle.list_tenants_with_skills_factory_enabled",
                new=AsyncMock(return_value=["should-not-be-called"]),
            ) as opted_in:
                result = await _list_tenants_for_action("archive-expired")
        assert result == ["a", "b"]
        active_all.assert_awaited_once()
        opted_in.assert_not_awaited()


# ── Publisher kwargs ──────────────────────────────────────────────


@pytest.mark.unit
class TestResolvePublisherKwargs:
    @pytest.mark.asyncio
    async def test_forge_distill_stamps_run_label(self):
        kwargs = await resolve_publisher_kwargs("forge-distill", "wet-test-tenant")
        assert "run_label" in kwargs
        # Deterministic format: ``forge-cron-<org>-<UTC YYYYMMDDtHHMM>``
        # so an operator inspecting an inbox card's ``origin.run_id``
        # can trace it back to the cron tick that minted it.
        assert re.match(
            r"^forge-cron-wet-test-tenant-\d{8}T\d{4}$", kwargs["run_label"]
        )

    @pytest.mark.asyncio
    async def test_archive_expired_returns_empty(self):
        kwargs = await resolve_publisher_kwargs("archive-expired", "any-tenant")
        assert kwargs == {}


# ── ForgeConfig resolution ────────────────────────────────────────


@pytest.mark.unit
class TestForgeConfigResolution:
    """The cron-tick must build ForgeConfig from per-tenant overrides
    (with sane fall-through to defaults)."""

    @pytest.mark.asyncio
    async def test_uses_tenant_overrides(self):
        fake_settings = {
            "skills_factory": {
                "body_max_bytes": 50_000,
                "description_max_bytes": 200,
                "forge": {
                    "min_cluster_size": 5,
                    "min_distinct_agents": 4,
                    "freshness_window_days": 7,
                    "max_writes_per_run": 10,
                },
            },
        }
        with patch(
            "core_api.services.forge.cron_handler.get_settings_for_display",
            new=AsyncMock(return_value=fake_settings),
        ):
            cfg = await _resolve_forge_config(org_id="tenant-1")
        assert cfg.min_cluster_size == 5
        assert cfg.min_distinct_agents == 4
        assert cfg.freshness_window_days == 7
        assert cfg.max_writes_per_run == 10
        assert cfg.body_max_bytes == 50_000
        assert cfg.description_max_bytes == 200

    @pytest.mark.asyncio
    async def test_falls_through_to_defaults(self):
        # Tenant with no overrides → ForgeConfig defaults.
        with patch(
            "core_api.services.forge.cron_handler.get_settings_for_display",
            new=AsyncMock(return_value={}),
        ):
            cfg = await _resolve_forge_config(org_id="empty-tenant")
        defaults = ForgeConfig()
        assert cfg.min_cluster_size == defaults.min_cluster_size
        assert cfg.body_max_bytes == defaults.body_max_bytes


# ── Cron-tick wiring ──────────────────────────────────────────────


@pytest.mark.unit
class TestRunForgeCronTick:
    """End-to-end: ``run_forge_cron_tick`` should call ``run_forge_distill``,
    then ``promote_pending_candidates``, and return their merged stats.
    """

    @pytest.mark.asyncio
    async def test_invokes_both_pipeline_phases_and_returns_stats(self):
        from core_api.services.forge.cron_handler import run_forge_cron_tick

        # Fake the heavy moving parts. The Forge service + promoter
        # have their own dedicated test suites — here we only verify
        # the cron adapter calls both, hands them the right config, and
        # surfaces the right numbers.
        fake_forge_result = ForgeRunResult(
            tenant_id="t1",
            fleet_id=None,
            window_start=None,  # unused in this test
            window_end=None,
            total_traces=0,
            labeled_traces=0,
            clusters_total=0,
            clusters_eligible=0,
            candidates_written=3,
            candidates_skipped_poisoned=1,
            candidates_skipped_sentinel=0,
            candidates_skipped_distill_error=0,
            candidates_skipped_io_error=0,
            candidates_skipped_existing=0,
            started_at=None,
            run_label="forge-cron-t1-20260608T2100",
            candidate_doc_ids=["forge/a", "forge/b", "forge/c"],
        )
        fake_promote_result = PromoterRunResult(
            tenant_id="t1",
            fleet_id=None,
            scanned=3,
            promoted=2,
            held=1,
            auto_approved=0,
        )

        with (
            patch(
                "core_api.services.forge.cron_handler.get_settings_for_display",
                new=AsyncMock(return_value={"skills_factory": {"forge": {}}}),
            ),
            patch(
                "core_api.services.forge.cron_handler._wire_llm_fn",
                new=AsyncMock(return_value=AsyncMock()),
            ),
            patch(
                "core_api.services.forge.cron_handler._make_candidate_writer",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler._make_status_checker",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.run_forge_distill",
                new=AsyncMock(return_value=fake_forge_result),
            ) as run_forge,
            patch(
                "core_api.services.forge.cron_handler.promote_pending_candidates",
                new=AsyncMock(return_value=fake_promote_result),
            ) as run_promote,
            patch(
                "core_api.services.forge.cron_handler.make_db_poison_checker",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.make_db_live_data_fetcher",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.make_db_status_updater",
                return_value=AsyncMock(),
            ),
        ):
            stats = await run_forge_cron_tick(
                tenant_id="t1",
                fleet_id=None,
                run_label="forge-cron-t1-20260608T2100",
            )

        # Both pipeline phases invoked.
        run_forge.assert_awaited_once()
        run_promote.assert_awaited_once()

        # Stats reflect both phases.
        assert stats["candidates_written"] == 3
        assert stats["promoted"] == 2
        assert stats["scanned"] == 3
        assert stats["held"] == 1
        # Skip counters surface from the Forge result.
        assert stats["skipped_poisoned"] == 1
        assert stats["skipped_sentinel"] == 0
        # auto_approved surfaces from the promoter result (0 here — the
        # flag defaults off because the patched settings omit it).
        assert stats["auto_approved"] == 0
        # Flag-off: promoter invoked with auto_promote_clean=False.
        assert run_promote.await_args.kwargs["auto_promote_clean"] is False

    @pytest.mark.asyncio
    async def test_auto_promote_clean_flag_threaded_when_enabled(self):
        from core_api.services.forge.cron_handler import run_forge_cron_tick

        fake_forge_result = ForgeRunResult(
            tenant_id="t1",
            fleet_id=None,
            window_start=None,
            window_end=None,
            total_traces=0,
            labeled_traces=0,
            clusters_total=0,
            clusters_eligible=0,
            candidates_written=1,
            candidates_skipped_poisoned=0,
            candidates_skipped_sentinel=0,
            candidates_skipped_distill_error=0,
            candidates_skipped_io_error=0,
            candidates_skipped_existing=0,
            started_at=None,
            run_label="forge-cron-t1-20260610T0000",
            candidate_doc_ids=["forge/x"],
        )
        fake_promote_result = PromoterRunResult(
            tenant_id="t1",
            fleet_id=None,
            scanned=1,
            promoted=1,
            held=0,
            auto_approved=1,  # the one candidate auto-activated
        )

        with (
            patch(
                "core_api.services.forge.cron_handler.get_settings_for_display",
                new=AsyncMock(
                    return_value={
                        "skills_factory": {
                            "forge": {},
                            "sentinel": {"auto_promote_clean": True},
                        }
                    }
                ),
            ),
            patch(
                "core_api.services.forge.cron_handler._wire_llm_fn",
                new=AsyncMock(return_value=AsyncMock()),
            ),
            patch(
                "core_api.services.forge.cron_handler._make_candidate_writer",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler._make_status_checker",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.run_forge_distill",
                new=AsyncMock(return_value=fake_forge_result),
            ),
            patch(
                "core_api.services.forge.cron_handler.promote_pending_candidates",
                new=AsyncMock(return_value=fake_promote_result),
            ) as run_promote,
            patch(
                "core_api.services.forge.cron_handler.make_db_poison_checker",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.make_db_live_data_fetcher",
                return_value=AsyncMock(),
            ),
            patch(
                "core_api.services.forge.cron_handler.make_db_status_updater",
                return_value=AsyncMock(),
            ),
        ):
            stats = await run_forge_cron_tick(
                tenant_id="t1",
                fleet_id=None,
                run_label="forge-cron-t1-20260610T0000",
            )

        # The flag from org_settings.sentinel.auto_promote_clean is
        # threaded into the promoter.
        assert run_promote.await_args.kwargs["auto_promote_clean"] is True
        assert stats["auto_approved"] == 1

    @pytest.mark.asyncio
    async def test_missing_llm_provider_raises_runtime_error(self):
        """The production cron MUST NOT silently substitute a fake LLM.
        If ``common.llm`` isn't importable the tick raises so the
        lifecycle handler marks the audit row ``failure`` (operator
        sees the misconfig).
        """
        from core_api.services.forge.cron_handler import _wire_llm_fn

        with patch(
            "core_api.services.forge.cron_handler.__import__",
            side_effect=ImportError("common.llm gone"),
            create=True,
        ):
            # Direct probe is simpler than patching ``common.llm`` in
            # sys.modules — _wire_llm_fn catches the ImportError at the
            # ``from common.llm import ...`` line.
            #
            # The real production path will hit the same branch when
            # the LLM provider chain is uninstalled.
            with pytest.raises(RuntimeError, match="common.llm not importable"):
                # Force the import by deleting the cached module first.
                import sys

                sys.modules.pop("common.llm", None)
                # Replace common with a stub missing ``llm``.
                with patch.dict(sys.modules, {"common.llm": None}):
                    await _wire_llm_fn()
