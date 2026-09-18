"""Service configuration — env vars validated at startup."""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "production", "sandbox"] = "development"

    log_level: str = "INFO"
    log_format_json: bool = False
    log_file: str = ""

    # When True, the scheduler skips registration and start. OSS standalone
    # deployments should not deploy core-operations at all; this flag is a
    # defensive short-circuit so that an accidentally-started instance
    # exits cleanly rather than firing cron jobs against a single-tenant
    # standalone DB.
    #
    # 09/02 L-45 — was ``standalone``, which binds the env var ``STANDALONE``.
    # Nothing sets that name. The variable every operator actually sets is
    # ``IS_STANDALONE``: .env.example, env.dev, .env.test, README, AGENT-INSTALL,
    # docs/, CI and core-api's own ``is_standalone`` all use it, and nothing in
    # the tree sets the bare spelling. With ``extra="ignore"`` above, pydantic
    # accepted ``IS_STANDALONE`` and discarded it — so the short-circuit below
    # could not fire, and a standalone deployment that started this image by
    # accident fired cron jobs at a single-tenant DB while the log line said
    # ``standalone: False``. Renaming loses no override precisely because the
    # old name was never set.
    is_standalone: bool = False

    # Timeout for this service's HTTP calls, all of which go to core-api.
    #
    # Sized ABOVE core-api's own request budget
    # (``core_api.config.Settings.request_timeout_seconds``, 45s, enforced by
    # RequestTimeoutMiddleware) so a slow sweep is WAITED OUT rather than
    # raced, and below the 120s gateway cap. tests/ asserts that ordering
    # against both real values rather than against a copy of either.
    #
    # Racing it is not a cosmetic problem. When the client gives up first the
    # POST raises, and ``_fire_fanout`` returns from the exception handler
    # WITHOUT reading the response body — where ``failed`` lives. That count is
    # the partial-sweep detector added after 2026-09-15, when every action
    # reported success while 38 orgs were dropped. A client timeout under the
    # server budget therefore disables that detector on precisely the slow
    # sweeps it exists to catch. Measured in prod on 2026-09-16: both archive
    # actions raised at 01:00:30, 28s after the 01:00:02 tick, while core-api
    # went on to finish the same work at 01:00:46.
    #
    # Was ``storage_http_timeout_s``, 30.0. Nothing in this service talks to
    # core-storage-api — all three call sites POST to core-api — so the name
    # described a dependency that is not there and the value was inherited
    # from one. Checked before renaming that no deploy workflow sets
    # STORAGE_HTTP_TIMEOUT_S, so nothing loses an override.
    core_api_http_timeout_s: float = 60.0

    # CAURA-655: core-operations doesn't talk to the DB directly — its
    # cron ticks POST to core-api's ``/admin/lifecycle/fanout/<action>``
    # endpoints, which do the org enumeration and Pub/Sub publish.
    core_api_url: str = "http://oss-core-api:8000"
    core_api_admin_api_key: str = ""

    # All lifecycle crons are wall-clock aligned to a fixed UTC hour
    # rather than a boot-relative interval: each runs once a day at its
    # configured hour, so it lands in a predictable off-peak window and
    # never drifts with redeploys (and never fires an immediate tick at
    # startup). Each knob is independently tunable so an operator can
    # stagger the jobs across the night; they all default to 02:00 UTC.
    # Per-org scheduling isn't supported here — a single global tick fans
    # out to every active org; enterprise's per-org configurable cadences
    # (e.g. ``security_audit.schedule_cron``) still live on its scheduler.

    # SQL-only archive ops (expired + stale share this hour).
    lifecycle_archive_run_at_hour: int = 2
    # Purge is operationally a different concern (compliance-driven
    # retention vs. staleness archival), so it gets its own hour and can
    # be moved off the archive slot.
    lifecycle_purge_run_at_hour: int = 2
    # Pipeline ops (crystallize + entity-link) — LLM-heavy, so an operator
    # may want these in their own off-peak slot away from the lighter SQL
    # ops. The consumer-side dedup gate still filters double-fires.
    lifecycle_pipeline_run_at_hour: int = 2
    # A72 — how often the crystallize tick fires, in hours. 24 keeps today's
    # single 02:00 run; lower values are the retune's third ground: a heavy
    # writing day outruns a daily sweep entirely, so everything written after
    # the tick waits ~24h for the janitor.
    #
    # Lowering this is affordable only because of the activity gate that landed
    # with it: a tenant with nothing written since its last COMPLETED sweep is
    # answered by two indexed aggregates in core-api and never reaches an LLM.
    # WITHOUT that gate this knob would multiply spend across every idle tenant,
    # which is why the two shipped together and why the default stays 24 — the
    # cadence is an operator's decision, not a deploy's.
    lifecycle_crystallize_every_hours: int = 24
    # Insights discovery (focus='discover'). Opt-in per-org
    # (``auto_insights_enabled``, default off); the consumer's activity
    # gate further no-ops ticks where no non-insight memories landed since
    # the last run, so a once-a-day fire is plenty.
    lifecycle_insights_run_at_hour: int = 2
    # Per-agent activity digest generation (CAURA-222 Phase 2). Opt-in per-org
    # (``agent_digest.enabled``, default off); a tenant that hasn't opted in
    # costs nothing, so a daily fire is safe.
    agent_digest_run_at_hour: int = 2
    # Weekly digest (period=week) — a separate wall-clock-aligned tick so the UI's
    # "week" toggle isn't dead. Fires once a week on ``weekday`` (0=Mon..6=Sun) at
    # ``hour``; default Monday 03:00 UTC, just after the Monday daily tick, so the
    # just-closed Mon-Mon window is covered.
    agent_digest_weekly_run_at_weekday: int = 0
    agent_digest_weekly_run_at_hour: int = 3
    # Generation runs INLINE in core-api (LLM per agent across opted-in orgs), so
    # its trigger POST can take minutes — a generous timeout, not the 30s default.
    agent_digest_http_timeout_s: float = 600.0

    # NULL-embedding re-embed sweep. DEFAULT OFF: the topic, durable
    # subscription and dead-letter topic are Terraform-provisioned (the bus
    # only auto-creates broadcast subscriptions), so until infra lands, a fire
    # would publish into a topic nothing consumes. Flip this on after
    # provisioning ``caura.lifecycle.embed-backfill-requested``.
    embed_backfill_enabled: bool = False
    # 04:00, deliberately NOT the 02:00 slot every other lifecycle tick
    # defaults to. That window is already congested enough that the nightly
    # cross-link call runs into the 120s Cloud Run request ceiling, and this
    # sweep feeds the same embedding backend those ticks compete for.
    embed_backfill_run_at_hour: int = 4

    @field_validator(
        "lifecycle_archive_run_at_hour",
        "lifecycle_purge_run_at_hour",
        "lifecycle_pipeline_run_at_hour",
        "lifecycle_insights_run_at_hour",
        "agent_digest_run_at_hour",
        "agent_digest_weekly_run_at_hour",
        "embed_backfill_run_at_hour",
    )
    @classmethod
    def _validate_run_at_hour(cls, v: int, info: ValidationInfo) -> int:
        if not 0 <= v <= 23:
            raise ValueError(f"{info.field_name} must be in 0..23 (UTC hour)")
        return v

    @field_validator("agent_digest_weekly_run_at_weekday")
    @classmethod
    def _validate_weekday(cls, v: int, info: ValidationInfo) -> int:
        if not 0 <= v <= 6:
            raise ValueError(f"{info.field_name} must be in 0..6 (Mon..Sun)")
        return v


settings = Settings()  # type: ignore[call-arg]
