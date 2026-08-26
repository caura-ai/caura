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

import contextlib
import re
from unittest.mock import AsyncMock, patch

import pytest

from common.events.base import PermanentOpError
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
            candidates_skipped_internal_error=0,
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
        # The bucket that means "we shipped a bug" has to reach the stats dict,
        # since that is the structured line an alert would key on.
        assert stats["skipped_internal_error"] == 0
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
            candidates_skipped_internal_error=0,
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
                # ``patch.dict`` alone, and the alone is the point: a
                # ``sys.modules`` entry of None makes ``import`` raise
                # ImportError, which is the branch under test, and patch.dict
                # restores the real module on exit.
                #
                # There used to be a ``sys.modules.pop("common.llm", None)``
                # here, OUTSIDE the patch.dict, to "force the import by
                # deleting the cached module first". It was both unnecessary
                # and unrestored: popping before patch.dict takes its snapshot
                # means the snapshot records "absent", so the real module never
                # came back and every later test in the session saw a
                # ``common.llm`` it had to re-import. That re-import rebinds the
                # package without rebinding its ``retry`` submodule attribute,
                # so an unrelated
                # ``monkeypatch.setattr("common.llm.retry.LLM_RETRY_DELAY_S", …)``
                # in tests/test_llm_provider_sdk_retries.py failed with
                # "'module' object at common.llm.retry has no attribute
                # 'retry'". It only started firing once _wire_llm_fn was fixed
                # to import successfully — before that its import always raised,
                # so nothing downstream ever completed the re-import.
                import sys

                with patch.dict(sys.modules, {"common.llm": None}):
                    await _wire_llm_fn()


# ── H-08: the injected callables must match the seams that call them ──


@pytest.mark.unit
class TestInjectedCallableArity:
    """The cron adapter's whole job is wiring, and a shape mismatch here is
    invisible to every other suite.

    ``run_forge_distill`` swallows exceptions from ``_distill_cluster`` into
    ``skipped_io_error`` so one bad cluster cannot abort a tick. That also means a
    ``TypeError`` from a wrongly-shaped injectable looks exactly like a storage
    hiccup: the tick returns success with zero candidates written. H-08 was that
    bug — the poison checker took ``(tenant, fleet, fp)`` while
    ``_distill_cluster`` calls ``await poison_checker(fingerprint.fp)`` — and it
    survived because the test above stubs ``run_forge_distill``, so nothing ever
    called what the cron handed it.

    These tests call the injectables through the same seam the service does.
    """

    @pytest.mark.asyncio
    async def test_make_poison_checker_takes_only_a_fingerprint(self):
        """The ``PoisonChecker`` seam is ``Callable[[str], Awaitable[bool]]``."""
        from core_api.services.forge.cron_handler import _make_poison_checker

        checker = _make_poison_checker("t1", "fleet-a")

        with patch(
            "core_api.services.forge.cron_handler.is_fingerprint_poisoned",
            new=AsyncMock(return_value=True),
        ) as is_poisoned:
            # Exactly how forge_service._distill_cluster invokes it.
            assert await checker("fp-abc123") is True

        # The identifiers must survive being closed over, or the check would
        # silently consult the wrong tenant's cooloff rows.
        is_poisoned.assert_awaited_once_with(
            tenant_id="t1", fleet_id="fleet-a", cluster_fingerprint="fp-abc123"
        )

    @pytest.mark.asyncio
    async def test_cron_hands_run_forge_distill_a_checker_it_can_actually_call(self):
        """The regression guard for H-08.

        Captures the callable the cron passes and invokes it the way the service
        does. Pre-fix this raised ``TypeError: _check() missing 2 required
        positional arguments``, which the service would have buried in
        ``skipped_io_error``.
        """
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
            candidates_written=0,
            candidates_skipped_poisoned=0,
            candidates_skipped_sentinel=0,
            candidates_skipped_distill_error=0,
            candidates_skipped_io_error=0,
            candidates_skipped_internal_error=0,
            candidates_skipped_existing=0,
            started_at=None,
            run_label="forge-cron-t1-20260819T1200",
            candidate_doc_ids=[],
        )
        fake_promote_result = PromoterRunResult(
            tenant_id="t1", fleet_id=None, scanned=0, promoted=0, held=0, auto_approved=0
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
            ),
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
            await run_forge_cron_tick(
                tenant_id="t1", fleet_id=None, run_label="forge-cron-t1-20260819T1200"
            )

            injected = run_forge.await_args.kwargs["poison_checker"]

            with patch(
                "core_api.services.forge.cron_handler.is_fingerprint_poisoned",
                new=AsyncMock(return_value=False),
            ) as is_poisoned:
                assert await injected("fp-from-the-seam") is False

        is_poisoned.assert_awaited_once_with(
            tenant_id="t1", fleet_id=None, cluster_fingerprint="fp-from-the-seam"
        )


# ── The tick's verdict must be truthful (#818 follow-up, round 2) ──


@pytest.mark.unit
class TestTickVerdict:
    """A tick that mined nothing because of a wiring bug used to return normally,
    so the shared lifecycle runner wrote ``status="success"``.

    That is not only dishonest: ``has_recent_lifecycle_success`` gates re-runs on a
    recent SUCCESS, so the false verdict also blocked the operator's next attempt
    for the 23h dedup window. Re-curling the fanout to reproduce H-08 would have
    returned "skipped: recent_success" and not run at all — the false success
    obstructed its own diagnosis.
    """

    @staticmethod
    def _forge_result(**over):
        base = dict(
            tenant_id="t1",
            fleet_id=None,
            window_start=None,
            window_end=None,
            total_traces=0,
            labeled_traces=0,
            clusters_total=0,
            clusters_eligible=2,
            candidates_written=0,
            candidates_skipped_poisoned=0,
            candidates_skipped_sentinel=0,
            candidates_skipped_distill_error=0,
            candidates_skipped_io_error=0,
            candidates_skipped_internal_error=0,
            candidates_skipped_existing=0,
            started_at=None,
            run_label="forge-cron-t1-20260820T0900",
            candidate_doc_ids=[],
        )
        base.update(over)
        return ForgeRunResult(**base)

    @staticmethod
    @contextlib.contextmanager
    def _patched(forge_result, promote_result):
        """Patch every dependency the tick touches; yields the two op mocks.

        Yielding them (rather than swallowing the handles) is what lets this stand
        in for the hand-rolled ``with (...)`` blocks the other classes in this file
        use — those assert on ``run_forge.await_args`` / ``run_promote.await_args``.
        """
        P = "core_api.services.forge.cron_handler."
        with (
            patch(
                P + "get_settings_for_display",
                new=AsyncMock(return_value={"skills_factory": {"forge": {}}}),
            ),
            patch(P + "_wire_llm_fn", new=AsyncMock(return_value=AsyncMock())),
            patch(P + "_make_candidate_writer", return_value=AsyncMock()),
            patch(P + "_make_status_checker", return_value=AsyncMock()),
            patch(P + "run_forge_distill", new=AsyncMock(return_value=forge_result)) as run_forge,
            patch(
                P + "promote_pending_candidates",
                new=AsyncMock(return_value=promote_result),
            ) as run_promote,
            patch(P + "make_db_poison_checker", return_value=AsyncMock()),
            patch(P + "make_db_live_data_fetcher", return_value=AsyncMock()),
            patch(P + "make_db_status_updater", return_value=AsyncMock()),
        ):
            yield run_forge, run_promote

    async def _tick(self, forge_result, promote_result):
        from core_api.services.forge.cron_handler import run_forge_cron_tick

        with self._patched(forge_result, promote_result):
            return await run_forge_cron_tick(
                tenant_id="t1", fleet_id=None, run_label="forge-cron-t1-20260820T0900"
            )

    _NO_PROMOTIONS = PromoterRunResult(
        tenant_id="t1", fleet_id=None, scanned=0, promoted=0, held=0, auto_approved=0
    )

    @pytest.mark.asyncio
    async def test_wrote_nothing_with_programming_errors_fails_the_tick(self):

        with pytest.raises(PermanentOpError) as exc:
            await self._tick(
                self._forge_result(candidates_skipped_internal_error=2), self._NO_PROMOTIONS
            )

        # The message has to name the counter and the scope, or the failure row's
        # error_message (truncated to 500 chars) tells on-call nothing actionable.
        assert "candidates_skipped_internal_error" in str(exc.value)
        assert "code/wiring bug" in str(exc.value)
        assert "t1" in str(exc.value)

    @pytest.mark.asyncio
    async def test_it_is_terminal_so_the_runner_will_not_redeliver(self):
        """Not a generic exception: a deterministic bug hits every cluster every
        tick, so redelivery only repeats the LLM spend before failing the same
        way. ``PermanentOpError`` is what tells the runner to ack."""

        with pytest.raises(PermanentOpError):
            await self._tick(
                self._forge_result(candidates_skipped_internal_error=1), self._NO_PROMOTIONS
            )

    @pytest.mark.asyncio
    async def test_internal_errors_alongside_a_written_candidate_do_not_fail_it(self):
        """One malformed cluster among successes is routine. Failing the tick over
        it would discard real work from the audit row and page someone nightly."""
        stats = await self._tick(
            self._forge_result(candidates_written=1, candidates_skipped_internal_error=1),
            self._NO_PROMOTIONS,
        )
        assert stats["candidates_written"] == 1
        assert stats["skipped_internal_error"] == 1

    @pytest.mark.asyncio
    async def test_a_genuinely_quiet_tick_still_succeeds(self):
        """Nothing to mine is not a failure — otherwise every idle tenant would
        fail its tick nightly and the signal would be worthless."""
        stats = await self._tick(self._forge_result(clusters_eligible=0), self._NO_PROMOTIONS)
        assert stats["candidates_written"] == 0
        assert stats["skipped_internal_error"] == 0

    @pytest.mark.asyncio
    async def test_io_errors_alone_do_not_fail_the_tick(self):
        """Storage having a bad day is exactly what the tick is meant to survive —
        and unlike a wiring bug, a retry genuinely might help."""
        stats = await self._tick(
            self._forge_result(candidates_skipped_io_error=2), self._NO_PROMOTIONS
        )
        assert stats["skipped_io_error"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permanent_error_survives_the_adapter_seam():
    """The raise has to cross ``_CoreApiLifecycleAdapter.forge_distill`` to reach
    the runner that acts on it.

    Worth its own test because that adapter is pure wiring, and pure wiring is
    exactly what went unwatched in H-08: the cron tests stubbed the seam, so a
    shape mismatch inside it was invisible to every suite. A stray ``try/except``
    added there later would silently restore the false-success behaviour this
    change exists to remove, and nothing else would notice.
    """
    from core_api.services.lifecycle_audit import _CoreApiLifecycleAdapter

    adapter = _CoreApiLifecycleAdapter(AsyncMock())
    boom = PermanentOpError("wrote nothing; wiring bug")

    with patch(
        "core_api.services.forge.cron_handler.run_forge_cron_tick",
        new=AsyncMock(side_effect=boom),
    ):
        with pytest.raises(PermanentOpError) as exc:
            await adapter.forge_distill(org_id="t1", fleet_id=None, run_label="r1")

    # Identity, not just type: a re-wrapped exception would lose the message the
    # failure row stores, and would no longer be the class the runner acks on.
    assert exc.value is boom
