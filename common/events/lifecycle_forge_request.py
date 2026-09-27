"""Typed payload for ``<brand>.lifecycle.forge-distill-requested`` (Skill Factory SF-007).

Per-run Forge invocation. Carries the standard four
:class:`LifecycleRequestBase` fields (``audit_id``, ``org_id``,
``triggered_by``, ``fleet_id``), a ``run_label``, and five per-run
override fields.

READ THIS BEFORE PUBLISHING AN OVERRIDE. Only ``org_id``, ``fleet_id``
and ``run_label`` reach the Forge run. ``lifecycle_handlers``'
``forge_distill_op`` passes exactly those three to
``PipelineStorageAdapter.forge_distill``, and the tick resolves every
bound it actually applies from ``org_settings.skills_factory.forge.*``
instead (``core_api.services.forge.cron_handler._resolve_forge_config``).
The four value overrides below are therefore INERT, and ``dry_run`` is
REFUSED rather than honoured — each field says which and why.

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
    """Forge distillation run request. ``run_label`` is consumed; the
    five override fields are not — see the module docstring for where
    the consumer stops reading.
    """

    # Free-form identifier for the run; surfaces on the audit row and
    # on every produced candidate's ``origin.run_id``. The Forge worker
    # is responsible for generating it before publishing; making it a
    # payload field (rather than auto-server-generated) lets test
    # harnesses pin it for deterministic replays.
    run_label: str

    # ── Per-run overrides: DECLARED, NOT CONSUMED (oss-0926-m-02) ──
    #
    # Field names mirror the configured knobs and
    # ``ForgeConfig.max_writes_per_run`` exactly so producers + the
    # consumer + the settings layer all spell the same thing, and
    # ``test_skill_schema_v1.TestForgeEventPayloadNaming`` pins that
    # spelling. The spelling is the only part of this block that is
    # currently load-bearing.
    #
    # Nothing reads these four. Setting one changes nothing about the
    # run — the tick takes every bound from the tenant's settings. They
    # are kept rather than deleted because they are IMPLEMENTABLE
    # (``ForgeConfig`` already carries all four) and because the naming
    # pin above requires ``max_writes_per_run`` to stay on the payload.
    # Contrast ``llm_tokens_per_run``, removed in #1729 precisely
    # because no honest implementation of it was available.
    #
    # Ignoring them fails SAFE, which is why they are documented here
    # rather than refused the way ``dry_run`` is: a run that falls
    # through to the configured defaults is bounded by values an
    # operator already chose, never by an unread event value.
    #
    # WHY THIS NOW SAYS SO OUT LOUD. It did say so once. The
    # consumer-side protocol comment read "extra run-knobs ... are
    # intentionally NOT plumbed through the adapter for the Phase 0
    # stub; Phase 1 will either fatten this signature or thread the
    # full request object." #311 deleted that sentence while wiring the
    # real tick and put nothing in its place — so from then until
    # oss-0926-m-02 the only description of these fields anywhere was
    # this file's, which said they worked.
    #
    # There is deliberately no token / cost override here. A
    # ``llm_tokens_per_run`` field sat in this list and was read by
    # nothing — not by this event's consumer and not by the settings
    # layer it mirrored — so it advertised a spend ceiling Forge has
    # never had (oss-0922-l-05). A run's LLM spend is bounded by how
    # many clusters it may ATTEMPT
    # (``skills_factory.forge.max_clusters_per_run``), because each
    # attempt buys exactly one distill call.
    #
    # ``max_clusters_per_run`` is deliberately NOT a field here, and
    # adding it is the obvious next thought: post-#1687 it is the knob
    # that actually bounds a run's spend, so an event carrying
    # ``max_writes_per_run`` and not it reads as an oversight. It stays
    # off until the overrides are consumed, because a sixth
    # declared-and-unread field would be a sixth instance of exactly
    # what this block exists to close. The control is not missing from
    # the system meanwhile — it is settable per tenant and IS read, at
    # ``cron_handler._resolve_forge_config``.
    freshness_window_days: int | None = None
    min_cluster_size: int | None = None
    min_distinct_agents: int | None = None
    max_writes_per_run: int | None = None

    # ``dry_run=True`` is REFUSED, not honoured and not ignored:
    # ``forge_distill_op`` raises ``PermanentOpError`` before the
    # adapter is touched, so the delivery is recorded as a terminal
    # ``failure`` row and no run happens.
    #
    # Singled out from the four above because ignoring it fails OPEN.
    # Its declared meaning was "produce candidates with
    # ``status=candidate`` only; do not run the staged-promotion
    # auto-gates", and the tick runs ``promote_pending_candidates``
    # unconditionally after mining — so an ignored dry run writes real
    # candidates AND promotes them, and under
    # ``skills_factory.sentinel.auto_promote_clean`` promotion can carry
    # a skill past ``staged`` to ``active``. A flag whose whole purpose
    # is to prevent side effects must not quietly produce the largest
    # one on offer.
    #
    # Refused rather than implemented because honouring it is not the
    # small change it looks like. Skipping the promotion half is easy.
    # The hard part is that a dry run which finishes normally writes a
    # ``success`` row for action ``forge-distill``, which the shared
    # handler's 23h dedup gate then reads as "already done" and uses to
    # skip the tenant's next REAL tick — so a dry run would silently
    # cost a day of live mining. Making that correct means changing a
    # dedup contract five other lifecycle actions share, which is a
    # design change rather than a remediation.
    dry_run: bool = False
