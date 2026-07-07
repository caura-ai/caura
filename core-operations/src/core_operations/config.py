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
    standalone: bool = False

    # core-storage-api URL for cron tasks that mutate data — the only
    # service permitted to touch the OSS DB directly. Defaults to the
    # local docker-compose service name.
    core_storage_api_url: str = "http://oss-core-storage-api:8002"

    storage_http_timeout_s: float = 30.0

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
    # Insights discovery (focus='discover'). Opt-in per-org
    # (``auto_insights_enabled``, default off); the consumer's activity
    # gate further no-ops ticks where no non-insight memories landed since
    # the last run, so a once-a-day fire is plenty.
    lifecycle_insights_run_at_hour: int = 2
    # Per-agent activity digest generation (CAURA-222 Phase 2). Opt-in per-org
    # (``agent_digest.enabled``, default off); a tenant that hasn't opted in
    # costs nothing, so a daily fire is safe.
    agent_digest_run_at_hour: int = 2
    # Generation runs INLINE in core-api (LLM per agent across opted-in orgs), so
    # its trigger POST can take minutes — a generous timeout, not the 30s default.
    agent_digest_http_timeout_s: float = 600.0

    @field_validator(
        "lifecycle_archive_run_at_hour",
        "lifecycle_purge_run_at_hour",
        "lifecycle_pipeline_run_at_hour",
        "lifecycle_insights_run_at_hour",
        "agent_digest_run_at_hour",
    )
    @classmethod
    def _validate_run_at_hour(cls, v: int, info: ValidationInfo) -> int:
        if not 0 <= v <= 23:
            raise ValueError(f"{info.field_name} must be in 0..23 (UTC hour)")
        return v


settings = Settings()  # type: ignore[call-arg]
