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


settings = Settings()  # type: ignore[call-arg]
