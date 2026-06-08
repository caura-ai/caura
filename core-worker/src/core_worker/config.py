"""Service configuration — env vars validated at startup."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "production", "sandbox"] = "development"

    # Logging
    log_level: str = "INFO"
    log_format_json: bool = False
    log_file: str = ""

    # Storage backend — the worker PATCHes embeddings to core-storage-api
    # via this URL. Defaults to the local docker-compose service name.
    core_storage_api_url: str = "http://oss-core-storage-api:8002"

    # Event bus — `inprocess` for tests/standalone OSS, `pubsub` for SaaS.
    event_bus_backend: Literal["inprocess", "pubsub"] = "inprocess"
    gcp_project_id: str = "local-dev"
    # One subscription per consumer service so a single topic can fan
    # out to multiple distinct subscribers; this prefix uniquely names
    # the worker's subscription on shared topics.
    event_bus_subscription_prefix: str = "core-worker"

    # HTTP timeout for the storage PATCH. The worker is off the request
    # hot path so a longer timeout is fine; we'd rather wait + succeed
    # than 504 + nack + redeliver.
    storage_http_timeout_s: float = 30.0

    # Per-tenant cap on concurrent storage PATCH-backs (embed/enrich
    # results). Keep aligned with core-api's
    # ``per_tenant_storage_write_concurrency`` so a single tenant's
    # combined occupancy of the storage-writer pool stays bounded
    # across both services. CAURA-636 — tenant-A storm fans into ~2
    # PATCHes per write (embed + enrich); without this cap one
    # tenant's burst saturates the pool and pushes other tenants into
    # 12x write-latency regression.
    per_tenant_storage_write_concurrency: int = 2

    @field_validator("per_tenant_storage_write_concurrency")
    @classmethod
    def _per_tenant_cap_must_be_positive(cls, v: int) -> int:
        # ``asyncio.Semaphore(0)`` is valid Python — no ValueError at
        # construction — but every ``acquire()`` would block forever
        # with no error or log, silently deadlocking every storage
        # PATCH-back. Reject at config load instead so the misconfig
        # surfaces at startup.
        if v < 1:
            raise ValueError(
                "per_tenant_storage_write_concurrency must be >= 1; 0 would deadlock every storage PATCH-back"
            )
        return v

    # Cloud Run port.
    port: int = 8080


# Module-level singleton — matches core-api's ``core_api.config.settings``
# pattern. Modules that need a config value should ``from core_worker.config
# import settings`` rather than reconstruct ``Settings()``: pydantic-settings
# isn't cached, so every reconstruction re-reads env + re-runs validators.
# Also lets call sites drop the ``# type: ignore[call-arg]`` that mypy needs
# at the construction site.
settings = Settings()  # type: ignore[call-arg]
