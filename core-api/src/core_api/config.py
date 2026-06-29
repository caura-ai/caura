import logging
from typing import Any, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


# Postgres connection settings. Canonical env var names follow the
# official ``postgres`` Docker image conventions (POSTGRES_USER,
# POSTGRES_PASSWORD, POSTGRES_DB) so the same ``.env`` works for both
# the database container and the app. Legacy ``ALLOYDB_*`` aliases are
# accepted for back-compat and will be dropped in a future major.
class Settings(BaseSettings):
    # NOTE: core-api holds no DB connection — all database access goes through
    # core-storage-api over HTTP (rule 6440b9a6). The former ``postgres_*`` /
    # ``db_pool_*`` settings + ``database_url`` were removed with the engine.
    api_key: str | None = None  # legacy, deprecated
    admin_api_key: str | None = None
    memclaw_api_key: str | None = None  # Optional: when set, all non-admin requests must present this key
    # Perimeter secret shared with the enterprise gateway. When set, the
    # header-trust auth path (X-Tenant-ID) additionally requires a matching
    # ``X-Gateway-Secret`` header — so a caller who reaches core-api directly
    # (bypassing the gateway via its public run.app URL) cannot impersonate a
    # tenant by setting identity headers itself. Unset (OSS/standalone/dev) = no-op.
    gateway_shared_secret: str | None = None
    embedding_provider: str = "openai"  # fake | openai | local
    # Per-deploy control for where embedding + LLM enrichment run.
    #
    # - ``"inline"`` (default): both embed + enrich run on the request
    #   path. Response includes LLM-derived fields (title, summary,
    #   tags, retrieval_hint) and ``CheckSemanticDuplicate`` runs
    #   against a real embedding. OSS-friendly — no worker fleet
    #   required. ``write_mode="strong"`` always forces this regardless
    #   of the deploy mode (CAURA-229 contract preserved by PR #151).
    # - ``"deferred"``: row persists with ``embedding=NULL`` + schema-
    #   default ``memory_type`` / ``weight`` / ``status``. Write
    #   publishes ``Topics.Memory.EMBED_REQUESTED`` and
    #   ``ENRICH_REQUESTED``; ``core-worker`` consumes both, runs the
    #   provider calls, and PATCHes the row. Search tolerates NULL
    #   embeddings via the FTS fallback (PR #150). Clients that need
    #   the LLM fields re-fetch after the back-channel ``ENRICHED``
    #   event lands. SaaS prod shape — sub-2s p99 SLA.
    #
    # F3 history: replaces the legacy ``embed_on_hot_path`` +
    # ``enrich_on_hot_path`` pair. Phase 1 introduced this field as a
    # derived alias; Phase 2 migrated 18 call sites to read it via the
    # ``inline_embedding`` / ``inline_enrichment`` helpers; Phase 3
    # (this revision) deleted the legacy flags + derivation validator.
    deployment_mode: Literal["inline", "deferred"] = "inline"
    # Reserved-agent-id write guard (`main` identity fix). Bare `agent_id="main"`
    # is the plugin's unset default; many installs collapse onto it. Phase 1:
    #   allow  → legacy behavior / instant rollback
    #   warn   → attribute as today but log `reserved_agent_write` (observe)
    #   reject → 409 with guidance; a write supplying a unique agent_id passes
    # Roll out warn → (measure) → reject. The bare-`main` delete gates on reject.
    reserved_agent_id_policy: Literal["allow", "warn", "reject"] = "warn"
    # Phase 2 (spoof hardening), ships dark: bind a write's agent_id to the
    # verified credential identity (auth.agent_id), ignoring a client-supplied
    # body override. Enable ONLY after the reserved-`main` credentials are
    # re-identified — otherwise it pins them back onto `main`.
    bind_write_identity_to_auth: bool = False
    # Outer cap on the inline embed+enrich gather in ParallelEmbedEnrich.
    # Was hardcoded at 20.0 — too tight under load once embedding moved
    # off the hot path (CAURA-594) and enrichment LLM became the sole
    # occupant. 35s leaves headroom for nano-class LLM tail latency
    # (typical p95 ~6-12s, plus 2 retries x 1s linear backoff) without
    # breaching the 45s outer request budget. Must stay below
    # ``request_timeout_seconds`` so this fires first.
    enrichment_inline_timeout_seconds: float = 35.0
    # Per-call timeout passed to the AsyncOpenAI client (covers both LLM
    # enrichment and embedding providers). Without an explicit value the
    # SDK rides httpx's default — long enough that a single hung upstream
    # call eats the whole enrichment budget silently. 25s gives the
    # provider room to respond while still leaving budget for one retry
    # under the inline ceiling.
    openai_request_timeout_seconds: float = 25.0
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    gemini_api_key: str | None = None
    entity_extraction_provider: str = "openai"  # none | fake | openai | anthropic | openrouter | gemini
    entity_extraction_model: str = "gpt-5.4-nano"
    use_llm_for_memory_creation: bool = True
    sentry_dsn: str = ""  # Set to enable Sentry error tracking
    redis_url: str = ""  # e.g. redis://localhost:6379/0. Empty = in-memory fallback.
    cors_origins: str = "http://localhost:3000"
    # Request-wide budget enforced by RequestTimeoutMiddleware. 45s fits
    # comfortably under the 120s gateway/Cloud Run cap (CAURA-623 raised
    # the nginx ``proxy_read_timeout`` from 60s to 120s; the staging
    # core-api Cloud Run service is also pinned to 120s at the platform
    # level), so a hung handler cannot keep a request slot past this.
    #
    # Residual risk: asyncio.timeout cancels the coroutine task but cannot
    # cancel sync threads started via asyncio.to_thread (Vertex / Gemini
    # provider SDKs). A hung provider holds its ThreadPoolExecutor slot
    # past the 504; size max_workers (lifespan in app.py) with that in
    # mind. Real fix is CAURA-594/595 (hot-path offload).
    #
    # Must stay >= BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS in constants.py
    # so the inner cap can actually fire before the outer one.
    request_timeout_seconds: float = 45.0
    # Bulk-only request budget (CAURA-602). The blanket 45s cap above
    # cancelled in-flight ``/memories/bulk`` calls *after* storage had
    # already committed, surfacing as silent creates on retry. Bulk
    # routes opt out of the global middleware (see ``app.py``) and
    # enforce this longer budget themselves; the per-attempt unique
    # constraint on ``memories.client_request_id`` makes a 504-here
    # retry-safe at the row level. p95 today is ~42s under load, so
    # 90s is roughly 2x headroom while staying 30s below the 120s
    # Cloud Run platform timeout (CAURA-623 — earlier comment cited
    # the 300s unconfigured default before the platform service was
    # pinned at 120s).
    bulk_request_timeout_seconds: float = 90.0
    # Per-phase cap on the storage roundtrip inside
    # ``create_memories_bulk`` (CAURA-599). Embedding and enrichment
    # already enforce their own 30s caps; storage was the only phase
    # without one, so a hung storage call ate the full
    # ``bulk_request_timeout_seconds`` umbrella before the 504 path
    # fired. The bulk path runs embed and enrich SEQUENTIALLY (embed
    # at memory_service.py:984, then enrich at the gather a few lines
    # below), so worst-case time before storage starts is
    # ``BULK_EMBEDDING_TIMEOUT + BULK_ENRICHMENT_TOTAL_TIMEOUT`` = 60s.
    # With a 90s umbrella that leaves 30s for storage, so 25s here
    # gives a 5s slack the validator enforces. Sized to fit the
    # observed p99 storage roundtrip (~3-5s under load) with ~5x
    # headroom for slow tails. The ``per_tenant_storage_slot`` acquire
    # is unbounded — this is the only deadline on the storage phase
    # itself.
    storage_bulk_timeout_seconds: float = 25.0
    # ``Retry-After`` header value (in seconds) on the 503 returned when
    # the storage call hits a network-level error (DNS, connect refused,
    # pool exhaustion not surfaced as TimeoutException). 5s is a balance
    # between letting the upstream recover and not stalling the client
    # for too long; tunable via env var so an operator can widen it
    # during a sustained outage to avoid thundering-herd retries.
    storage_network_error_retry_after_seconds: int = 5
    # Audit-event queue tunables (CAURA-628). The queue replaces the
    # legacy per-event audit POST with batched flushes, removing the
    # cross-tenant table-lock contention that surfaced as the residual
    # noisy-neighbor-write signal after the LLM-pool fix in #34 and
    # the dead-index drop in #35.
    #
    # ``audit_queue_max_size``: per-process queue cap. With ~200 bytes
    # per event, 10000 events fits in ~2 MiB. Sized well over realistic
    # storm rate so the queue only fills if storage-api is degraded;
    # overflow triggers a structured warning + drop counter rather
    # than blocking the request hot path.
    #
    # ``audit_queue_flush_threshold``: events accumulated → flush
    # immediately. 50 keeps the average batch comfortable for one
    # multi-row INSERT without stalling steady-state low-volume
    # tenants behind a long flush cadence.
    #
    # ``audit_queue_flush_interval_seconds``: maximum staleness for
    # any single event before it lands in storage. 1s matches the
    # CAURA-627 scoping recommendation; the audit_list endpoint may
    # see up to that delay for the most recent events, which is
    # acceptable for the post-hoc analysis paths that consume it.
    #
    # Setting ``audit_queue_max_size = 0`` disables the queue entirely
    # — ``log_action`` then falls through to the legacy synchronous
    # POST. Useful as an incident-time kill-switch without a redeploy.
    audit_queue_max_size: int = 10000
    audit_queue_flush_threshold: int = 50
    audit_queue_flush_interval_seconds: float = 1.0
    # Capability-usage adoption counters (services/capability_usage.py).
    # In-process aggregation flushed to the ``capability_usage`` table on
    # this interval — the data behind the per-capability / per-transport /
    # per-org adoption report. Set ``capability_usage_enabled = False`` to
    # turn off recording (the aggregator is never started, record_usage()
    # becomes a no-op).
    capability_usage_enabled: bool = True
    capability_usage_flush_interval_seconds: float = 15.0
    # Rate limits applied per-route via slowapi decorators
    # (middleware/rate_limit.py). Syntax: "<count>/<period>" where period
    # is second | minute | hour | day.
    # Mirrors the nginx gateway shape (write_zone 10/s, api_zone 30/s)
    # but keyed by API key rather than IP.
    rate_limit_write: str = "10/second"
    # Bulk write fans out to BULK_MAX_ITEMS=100 memories per request, so a
    # stricter request-level cap keeps the effective memory-write ceiling
    # aligned with the single-write path (2/s * 100 = 200/s vs 10/s single).
    rate_limit_write_bulk: str = "2/second"
    rate_limit_search: str = "30/second"
    # Per-tenant in-flight concurrency caps (see
    # ``middleware/per_tenant_concurrency.py`` for full rationale).
    # Per-instance state — fleet-wide cap is roughly
    # ``cap * max_instances``. Sized to absorb routine per-tenant
    # fan-out (the harness's microbench phase issues ~10-30 concurrent
    # search/list ops) while still tripping under a genuine storm.
    per_tenant_search_concurrency: int = 32
    per_tenant_write_concurrency: int = 16
    # Per-tenant cap on concurrent embedding-backend (TEI) calls. Gates
    # only the single-flight cold-miss leader in
    # ``memory_service._get_or_cache_embedding`` (cache hits / in-flight
    # joiners take no slot), so one hot tenant's search storm can't
    # occupy the whole embedding service and starve other tenants
    # (noisy-neighbor-search). The TEI backend is a fixed pool
    # (``staging-memclaw-tei``: 2 instances x containerConcurrency 10 =
    # ~20 slots, no autoscale); with cap N on M core-api instances a
    # single tenant holds at most ``N * M`` of those, leaving headroom
    # for everyone else. Tighter than ``per_tenant_search_concurrency``
    # on purpose: a tenant may have many searches in flight but only a
    # few concurrent cold embeds. Fast-fails 429 like the other
    # route-entry caps rather than queueing behind TEI.
    per_tenant_embed_concurrency: int = 6
    # Deeper bulkhead at the storage roundtrip itself
    # (CAURA-602 follow-up). Smaller than the route-entry caps above
    # because each request only holds the storage slot for the actual
    # roundtrip (~500ms-3s), not for the whole embed/enrich/storage
    # cycle. Sizing target: ``per_tenant_storage_write_concurrency *
    # max_instances`` should sit comfortably below the storage-writer
    # pool size (10/instance x 11 = 110 fleet-wide today) so a single
    # tenant can't park more than ~20% of pool slots. Acquire is
    # unbounded — a saturated tenant queues here while the route
    # budget caps total wait time; see ``per_tenant_storage_slot``.
    per_tenant_storage_write_concurrency: int = 2
    per_tenant_storage_search_concurrency: int = 4
    # Fail-fast budget when the cap is exhausted. Long enough to absorb
    # a benign race between two near-simultaneous arrivals; short
    # enough that real exhaustion fails before the request hits the
    # worker.
    per_tenant_acquire_timeout_seconds: float = 0.05
    # Idempotency-Key inbox TTL. 24h matches Stripe's default and is
    # longer than any realistic client retry budget. Cached responses
    # older than this are treated as absent and the request re-runs.
    idempotency_ttl_seconds: int = 86400
    # TTL for pending claims (rows with ``is_pending=True`` waiting for
    # the handler to call ``record()``). MUST be much shorter than
    # ``idempotency_ttl_seconds``: a crashed/timed-out handler leaves the
    # row pending; without a short TTL it would soft-ban the key for the
    # full 24h. The expired-row reclaim path in ``idempotency_claim``
    # auto-recovers stuck pending rows once this TTL elapses. Sized
    # generously above realistic handler latency (single write <2s, bulk
    # write <60s, search <2s).
    idempotency_pending_ttl_seconds: int = 90
    environment: Literal["development", "production", "sandbox"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # JSON output by default so Cloud Logging picks up severity/message.
    # Local developers can set LOG_FORMAT_JSON=false for structlog's
    # coloured ConsoleRenderer.
    log_format_json: bool = True
    # On-prem deployments set this to /var/log/memclaw/core-api/core-api.log so
    # logs land on disk too (daily-rotated, 5-day retention). Empty = stdout only.
    log_file: str = ""
    # Default False: standalone=True bypasses tenant auth, so it must be an explicit opt-in.
    is_standalone: bool = False
    crystallizer_enabled: bool = True
    crystallizer_stale_days: int = 180
    crystallizer_dedup_sample_size: int = 1000
    crystallizer_dedup_threshold: float = 0.95
    core_storage_api_url: str = "http://localhost:8002"
    # Enterprise SaaS splits core-storage-api into writer + reader Cloud Run
    # services (CAURA-591 Part B). When this is set, the storage client
    # routes GET + tagged-read POST calls here instead of ``core_storage_api_url``;
    # empty keeps today's single-service behaviour (OSS + pre-split deploys).
    core_storage_read_url: str = ""
    settings_encryption_key: str = ""  # Required in production (Fernet key)
    jwt_secret: str = "change-me-in-production"  # Required in production
    paddle_client_token: str | None = None
    paddle_environment: str = "sandbox"
    paddle_webhook_secret: str | None = None
    paddle_pro_monthly_price_id: str | None = None
    paddle_pro_annual_price_id: str | None = None
    paddle_business_monthly_price_id: str | None = None
    paddle_business_annual_price_id: str | None = None
    use_stm: bool = False
    stm_backend: str = "memory"  # memory | redis
    stm_notes_ttl: int = 86400  # 24h
    stm_bulletin_ttl: int = 172800  # 48h
    payment_provider: str = "paddle"

    # Platform default providers — Caura's own API keys for tenants without credentials.
    # Set these in enterprise deployments; leave empty for OSS self-hosted.
    platform_llm_provider: str = ""  # "vertex" | "openai" | "" (disabled)
    platform_llm_model: str = ""  # e.g. "gemini-3.1-flash-lite-preview"
    platform_llm_api_key: SecretStr = SecretStr("")  # OpenAI LLM: API key
    platform_llm_gcp_project_id: str = ""  # Vertex: GCP project
    platform_llm_gcp_location: str = ""  # Vertex: region
    platform_embedding_provider: str = ""  # "openai" | "" (disabled)
    platform_embedding_api_key: SecretStr = SecretStr("")  # OpenAI: API key for embeddings
    platform_embedding_model: str = ""  # e.g. "text-embedding-3-small"

    # Security audit — scheduler + threshold alerts. Enterprise-only feature;
    # OSS standalone deployments can leave these at defaults (all off).
    # Per-org overrides live in organization_settings.security_audit.
    security_audit_schedule_enabled: bool = False
    security_audit_schedule_cron: str = "0 2 * * *"  # daily 02:00 by default
    security_audit_alerts_enabled: bool = False
    security_audit_alert_recipients: list[str] = []  # comma-separated env → list
    security_audit_alert_score_below: float | None = None
    security_audit_alert_critical_findings_min: int | None = None
    security_audit_alert_score_drop_delta: float | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: Any) -> Any:
        # Pydantic's Literal check rejects invalid values with a clear error
        # after we return — this validator only needs to uppercase so env
        # vars like LOG_LEVEL=debug are accepted.
        return v.upper() if isinstance(v, str) else v

    @field_validator("security_audit_alert_recipients", mode="before")
    @classmethod
    def _split_recipients(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _validate_timeout_ordering(self) -> "Settings":
        # Local import avoids a circular: constants → common.constants,
        # but this file is imported by constants.py's dependents.
        from core_api.constants import (
            BULK_EMBEDDING_TIMEOUT_SECONDS,
            BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS,
        )

        if self.request_timeout_seconds < BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(
                f"request_timeout_seconds ({self.request_timeout_seconds}s) must be >= "
                f"BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS ({BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS}s) "
                "so the inner enrichment cap can fire before the outer request budget."
            )
        if self.enrichment_inline_timeout_seconds >= self.request_timeout_seconds:
            raise ValueError(
                f"enrichment_inline_timeout_seconds ({self.enrichment_inline_timeout_seconds}s) "
                f"must be < request_timeout_seconds ({self.request_timeout_seconds}s) so the "
                "inline embed+enrich cap fires before the outer request budget."
            )
        if self.bulk_request_timeout_seconds < BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS:
            # Bulk runs its own budget, so the same inner-fires-first
            # rule has to hold here. If enrichment can't finish under
            # the bulk budget, the only safe outcome is a 504 with the
            # per-attempt-id retry path.
            raise ValueError(
                f"bulk_request_timeout_seconds ({self.bulk_request_timeout_seconds}s) "
                f"must be >= BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS "
                f"({BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS}s)."
            )
        if (
            self.storage_bulk_timeout_seconds
            + BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS
            + BULK_EMBEDDING_TIMEOUT_SECONDS
            >= self.bulk_request_timeout_seconds
        ):
            # The per-phase storage cap (CAURA-599) must fire before the
            # umbrella. The bulk path runs embed (line 984 in
            # memory_service.py) THEN enrich (line 1018) THEN storage
            # SEQUENTIALLY, so worst-case wall-clock before storage even
            # starts is ``embed + enrich``. Checking ``storage <
            # bulk_request`` alone would admit configs where
            # ``embed + enrich + storage > bulk_request`` and the
            # umbrella silently fires first, defeating the per-phase
            # cleanup.
            raise ValueError(
                f"storage_bulk_timeout_seconds ({self.storage_bulk_timeout_seconds}s) "
                f"+ BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS "
                f"({BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS}s) "
                f"+ BULK_EMBEDDING_TIMEOUT_SECONDS "
                f"({BULK_EMBEDDING_TIMEOUT_SECONDS}s) must be < "
                f"bulk_request_timeout_seconds "
                f"({self.bulk_request_timeout_seconds}s) so the storage-phase "
                f"cap fires before the umbrella."
            )
        return self

    @field_validator("security_audit_schedule_cron")
    @classmethod
    def _validate_cron_field(cls, v: str) -> str:
        from croniter import CroniterBadCronError, croniter

        try:
            croniter(v)
        except (CroniterBadCronError, ValueError) as exc:
            raise ValueError(f"Invalid cron expression {v!r}: {exc}") from exc
        return v

    @model_validator(mode="after")
    def _remap_deprecated_vertex(self) -> "Settings":
        """Graceful fallback for deprecated tenant-tier ``vertex`` provider.

        Vertex is now platform-tier only. If a deployment still has
        ``EMBEDDING_PROVIDER=vertex`` or ``ENTITY_EXTRACTION_PROVIDER=vertex``
        from a pre-migration env file, remap to ``openai`` at startup rather
        than crashing on the first request through ``get_*_provider``.

        String literals (``"vertex"`` / ``"openai"``) are used here rather
        than ``ProviderName`` members because this validator runs during
        ``settings = Settings()`` at module-bottom import time, and importing
        ``core_api.providers._names`` triggers ``core_api.providers.__init__``
        which imports back from ``core_api.config`` — a circular. StrEnum
        equality means the comparison still works for any future enum-aware
        callers.
        """
        if self.embedding_provider == "vertex":
            logger.warning(
                "EMBEDDING_PROVIDER=vertex is no longer supported (CAURA-333: "
                "Vertex embeddings never passed output_dimensionality and were "
                "rejected by pgvector's 1024-dim column). Remapping to 'openai'."
            )
            object.__setattr__(self, "embedding_provider", "openai")
            # A user coming from Vertex likely has no OPENAI_API_KEY — without a
            # key the registry silently falls back to FakeEmbeddingProvider,
            # which breaks semantic search with no clear signal. Escalate.
            if not self.openai_api_key and not self.platform_embedding_provider:
                logger.error(
                    "EMBEDDING_PROVIDER was remapped from 'vertex' to 'openai', but "
                    "OPENAI_API_KEY is unset and PLATFORM_EMBEDDING_PROVIDER is not "
                    "configured. Semantic search will use FakeEmbeddingProvider and "
                    "produce zero-vectors. Set OPENAI_API_KEY or configure "
                    "PLATFORM_EMBEDDING_PROVIDER=openai to restore embeddings."
                )
        if self.entity_extraction_provider == "vertex":
            logger.warning(
                "ENTITY_EXTRACTION_PROVIDER=vertex is no longer supported as a "
                "tenant-facing provider. Remapping to 'openai'. Configure platform-tier "
                "Vertex via PLATFORM_LLM_PROVIDER instead."
            )
            object.__setattr__(self, "entity_extraction_provider", "openai")
            # Less catastrophic than fake embeddings (LLM enrichment degrades
            # to heuristics) but still worth flagging prominently.
            if not self.openai_api_key and not self.platform_llm_provider:
                logger.error(
                    "ENTITY_EXTRACTION_PROVIDER was remapped from 'vertex' to 'openai', "
                    "but OPENAI_API_KEY is unset and PLATFORM_LLM_PROVIDER is not "
                    "configured. LLM enrichment will use FakeLLMProvider. Set "
                    "OPENAI_API_KEY or configure PLATFORM_LLM_PROVIDER to restore "
                    "enrichment."
                )
        return self

    @property
    def inline_embedding(self) -> bool:
        """True iff the resolved deployment mode runs embedding inline."""
        return self.deployment_mode == "inline"

    @property
    def inline_enrichment(self) -> bool:
        """True iff the resolved deployment mode runs enrichment inline."""
        return self.deployment_mode == "inline"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()


def bridge_credentials_to_environ() -> None:
    """Copy ``settings.X`` credential values into ``os.environ``.

    pydantic-settings reads ``.env`` files into the ``Settings``
    instance but does NOT export those values back into
    ``os.environ``. ``common.llm._credentials`` and
    ``common.llm._platform`` (CAURA-595 extraction) read
    ``os.environ`` directly — by design, so core-worker can use them
    without depending on pydantic-settings.

    Without this bridge, a developer with ``OPENAI_API_KEY=sk-...`` in
    ``.env`` (the documented local-dev shape) would silently get
    ``FakeLLMProvider`` for all enrichment / entity-extraction /
    contradiction-detection LLM calls; same for the platform-tier
    singletons configured by ``PLATFORM_*`` settings. The bridge runs
    once during the FastAPI lifespan startup before
    ``init_platform_providers()``.

    Idempotent: only sets keys that aren't already in ``os.environ``,
    so an explicit shell export wins over the ``.env`` value (matches
    pydantic-settings' own precedence: env > ``.env``).
    """
    import os

    bridges: dict[str, str] = {
        # Tenant-tier provider keys read by ``common.llm._credentials``.
        "OPENAI_API_KEY": settings.openai_api_key or "",
        "ANTHROPIC_API_KEY": settings.anthropic_api_key or "",
        "OPENROUTER_API_KEY": settings.openrouter_api_key or "",
        "GEMINI_API_KEY": settings.gemini_api_key or "",
        # Default provider + model used by ``common.enrichment.service``.
        "ENTITY_EXTRACTION_PROVIDER": settings.entity_extraction_provider or "",
        "ENTITY_EXTRACTION_MODEL": settings.entity_extraction_model or "",
        # OpenAI client timeout used by ``common.llm.constants``.
        "OPENAI_REQUEST_TIMEOUT_SECONDS": str(settings.openai_request_timeout_seconds),
        # Platform-tier singletons read by ``common.llm._platform``.
        "PLATFORM_LLM_PROVIDER": settings.platform_llm_provider or "",
        "PLATFORM_LLM_MODEL": settings.platform_llm_model or "",
        "PLATFORM_LLM_API_KEY": (
            settings.platform_llm_api_key.get_secret_value() if settings.platform_llm_api_key else ""
        ),
        "PLATFORM_LLM_GCP_PROJECT_ID": settings.platform_llm_gcp_project_id or "",
        "PLATFORM_LLM_GCP_LOCATION": settings.platform_llm_gcp_location or "",
    }
    for env_name, value in bridges.items():
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value
