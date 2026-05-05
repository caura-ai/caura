"""Service configuration — env vars validated at startup."""

from __future__ import annotations

from typing import Literal

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

    # Daily cadence for the SQL-only archive operations. Per-org
    # scheduling isn't supported here (single global tick fans out to
    # every active org); enterprise's per-org configurable cadences
    # — e.g. ``security_audit.schedule_cron`` — still live on the
    # enterprise scheduler.
    lifecycle_archive_interval_seconds: float = 24 * 3600
    # Separate cadence for purge — operationally a different concern
    # (compliance-driven retention vs. staleness archival), so an
    # operator can dial purge less frequently or to a different cycle
    # without touching archive. Default matches archive (daily).
    lifecycle_purge_interval_seconds: float = 24 * 3600


settings = Settings()  # type: ignore[call-arg]
