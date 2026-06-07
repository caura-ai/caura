"""Typed payload for ``memclaw.lifecycle.forge-distill-requested`` (Skill Factory SF-007).

Per-run Forge invocation. Carries the standard four
:class:`LifecycleRequestBase` fields (``audit_id``, ``org_id``,
``triggered_by``, ``fleet_id``) plus run-knobs that override the
tenant defaults from ``org_settings.skills_factory.forge.*`` when the
publisher wants tighter / looser bounds for this particular run
(e.g. an operator-initiated dry-run with a much wider freshness
window). Leaving every override at ``None`` means "use the configured
defaults" and is the production path.

A Forge run is identified by ``run_label`` (free-form string supplied
by the publisher, e.g. ``forge-cron-20260510-0600`` or
``forge-dry-run-eldad-${ulid}``); it shows up in the audit row and on
every candidate doc the run produces (``origin.run_id``). This lets us
attribute a candidate back to the precise Forge run that emitted it
even after re-mining produces v2 proposals.
"""

from __future__ import annotations

from common.events.lifecycle_archive_request import LifecycleRequestBase


class LifecycleForgeDistillRequest(LifecycleRequestBase):
    """Forge distillation run request. Run-knobs default to ``None``
    so omitted fields fall through to ``org_settings.skills_factory.forge.*``.
    """

    # Free-form identifier for the run; surfaces on the audit row and
    # on every produced candidate's ``origin.run_id``. The Forge worker
    # is responsible for generating it before publishing; making it a
    # payload field (rather than auto-server-generated) lets test
    # harnesses pin it for deterministic replays.
    run_label: str

    # Per-run overrides for the configured knobs. ``None`` ⇒ use the
    # tenant default from ``org_settings.skills_factory.forge.*``.
    # Field names mirror those configured knobs and
    # ``ForgeConfig.max_writes_per_run`` exactly so producers + the
    # consumer + the settings layer all spell the same thing.
    freshness_window_days: int | None = None
    min_cluster_size: int | None = None
    min_distinct_agents: int | None = None
    llm_tokens_per_run: int | None = None
    max_writes_per_run: int | None = None
    # ``dry_run=True`` ⇒ produce candidates with ``status=candidate``
    # only; do not run the staged-promotion auto-gates. Used by the
    # ``memclawctl forge dry-run`` CLI in Phase 1 and by the eval
    # harness. Default ``False`` is the production path.
    dry_run: bool = False
