# API reference

Versioned REST routes live under `/api/v1/`; MCP is mounted separately at
`/mcp`. A running deployment serves its authoritative OpenAPI schema at
`/api/openapi.json` and interactive Swagger docs at `/api/docs`. The tables below
are a curated map; use the schema for exact request and response shapes.

See also the [public API stability contract](public-api-stability.md) and the
[API surface ownership charter](api-surfaces.md).

<details>
<summary>Memory endpoints</summary>

| Endpoint | Method | Description |
|---|---|---|
| `/memories` | POST | Write a memory. LLM enrichment + embedding + entity extraction + contradiction detection. `"persist": false` for extract-only preview |
| `/memories/bulk` | POST | Write up to 100 memories. Batches embeddings, parallelizes enrichment, single transaction. Requires `X-Bulk-Attempt-Id` header (per-attempt idempotency); a retry with the same id resolves committed rows as `duplicate_attempt` instead of duplicating. Returns 200 (clean / all-error) or 207 Multi-Status (mixed) — read per-item `status` |
| `/memories` | GET | List memories (filter by type, status, agent; paginate) |
| `/memories/{id}` | GET | Full memory detail (embedding stats, entity links, RDF triple, temporal bounds) |
| `/memories/{id}` | PATCH | Update content or metadata. Re-embeds if content changes |
| `/memories/{id}` | DELETE | Soft delete (sets status to `deleted`) |
| `/memories/{id}/status` | PATCH | Update lifecycle status |
| `/memories/{id}/contradictions` | GET | View contradiction chain |
| `/memories` | DELETE | Bulk soft-delete |
| `/memories/stats` | GET | Counts by type, agent, and status |
| `/search` | POST | Hybrid semantic + keyword search with graph-enhanced retrieval |
| `/recall` | POST | Search + LLM synthesis — `summary` is the answer to the query (the model reasons step by step internally; only its final answer is surfaced), alongside the source memories under both `memories` and `items` |
| `/ingest/preview` | POST | Extract 5-20 atomic facts from a URL or text (no writes) |
| `/ingest/commit` | POST | Write previewed facts as memories |

</details>

<details>
<summary>Knowledge graph endpoints</summary>

| Endpoint | Method | Description |
|---|---|---|
| `/entities` | GET | List entities (filter by type, search) |
| `/entities/upsert` | POST | Create or update entity |
| `/entities/{id}` | GET | Entity detail with relations and linked memories |
| `/relations/upsert` | POST | Create or update relation |
| `/graph` | GET | Full knowledge graph (entities + relations) |

</details>

<details>
<summary>Evolve, Insights, Agents, Crystallizer, Documents, Fleet, Admin</summary>

**Karpathy Loop / Evolve**

| Endpoint | Method | Description |
|---|---|---|
| `/evolve/report` | POST | Report an outcome (success/failure/partial) against recalled memories |

**Insights**

| Endpoint | Method | Description |
|---|---|---|
| `/insights/generate` | POST | LLM-powered analysis. Focus: `contradictions`, `failures`, `stale`, `divergence`, `patterns`, `discover` |

**Agents**

| Endpoint | Method | Description |
|---|---|---|
| `/agents` | GET | List registered agents with trust levels |
| `/agents/{id}` | GET | Single agent detail |
| `/agents/{id}/trust` | PATCH | Set trust level (0-3) |

**Memory Crystallizer**

| Endpoint | Method | Description |
|---|---|---|
| `/crystallize` | POST | Trigger crystallization for a tenant |
| `/crystallize/all` | POST | Trigger for all tenants (admin key, nightly) |
| `/crystallize/reports` | GET | List crystallization reports |
| `/crystallize/latest` | GET | Most recent completed report |

**Documents**

| Endpoint | Method | Description |
|---|---|---|
| `/documents` | POST | Store or update a structured JSON document |
| `/documents/{id}` | GET | Retrieve document by ID |
| `/documents/query` | POST | Query by field equality filters |
| `/documents/{id}` | DELETE | Delete a document |

**Fleet**

| Endpoint | Method | Description |
|---|---|---|
| `/fleet/heartbeat` | POST | Plugin heartbeat — upserts node status, returns pending commands |
| `/fleet/nodes` | GET | List fleet nodes with status (online/stale/offline) |
| `/fleet/commands` | POST | Queue a command for a node |
| `/fleet/commands` | GET | List command history |

**Admin + System**

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check |
| `/version` | GET | Current version |
| `/tool-descriptions` | GET | Canonical MCP tool descriptions |
| `/admin/tenants` | GET | List all tenants (admin key) |
| `/admin/fleets` | GET | List fleets across all tenants (admin key) |
| `/admin/memories` | GET | List memories across all tenants with filters (admin key) |
| `/admin/memories/stats` | GET | Memory counts by tenant/type/status (admin key) |
| `/settings` | GET / PUT | Per-tenant configuration |
| `/audit-log` | GET | Audit log entries |
| `/mcp` | POST | MCP Streamable HTTP endpoint (mounted at app root, NOT under `/api/v1`) |

**Auth:** Most data endpoints require an `X-API-Key`; admin endpoints require
the admin key. Intentional public exceptions include the health/version/tool
description probes, `/api/v1/whoami`, and the plugin/skill bootstrap routes
(`/api/v1/plugin-*`, `/api/v1/install-*`, and `/api/v1/skill/*`). These public
routes expose generic software or identity-probe data, not tenant data.

**Gateway-injected headers** (trusted only behind the enterprise gateway):

| Header | Effect |
|---|---|
| `X-Agent-ID` | Scopes the request to this agent |
| `X-Org-Read-Only: true` | Read-only mode — creates/updates return 403 |
| `X-Tenant-ID` | Tenant identity when using the shared `CAURA_API_KEY` gate |

The identity headers are trusted on the gateway-header auth path. Set
`GATEWAY_SHARED_SECRET` so that path also requires a matching
`X-Gateway-Secret`. A network-exposed OSS deployment without a gateway should
set `CAURA_API_KEY`; that shared-key path authenticates first and prevents the
header-trust path from being reached.

**Rate limiting (managed platform)**

These limits apply to the managed platform at `caura.ai`. A self-hosted deployment enforces its own, looser per-route limits out of the box — see the [self-hosted rate limiting](../README.md#rate-limiting) section.

| Scope | Limit |
|---|---|
| Memory writes | 60 req/min per API key |
| Memory searches | 120 req/min per API key |
| General reads | 300 req/min per API key |
| Auth endpoints | 10 req/min per IP |
| Global DDoS floor | 1000 req/min per IP |

Exceeded limits return HTTP 429 with a `Retry-After` header. Rate-limited routes also carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` on **successful** responses, so a client can back off before it is throttled rather than after.

</details>

## Configuration

Configuration is supplied through environment variables or `.env`. See
[`.env.example`](../.env.example) for the common OSS settings; the table below
also includes production-only safety controls.

The stock Compose file sets the storage service's `DATABASE_URL` to its bundled
PostgreSQL service. A custom storage deployment should set `DATABASE_URL`
directly. A complete `ALLOYDB_HOST`, `ALLOYDB_USER`, `ALLOYDB_PASSWORD`, and
`ALLOYDB_DATABASE` set (plus optional `ALLOYDB_PORT`) is also supported when
`DATABASE_URL` is absent; these are storage-service inputs, not aliases for the
`POSTGRES_*` fields.

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | local PostgreSQL defaults | Inputs used by migration/dev helpers; the stock Compose file hardcodes its container connection values |
| `DATABASE_URL` | local PostgreSQL URL | Storage-service primary connection URL; set directly outside the stock Compose deployment |
| `READ_DATABASE_URL` | *(empty)* | Optional storage-service read-replica URL |
| `ADMIN_API_KEY` | *(empty)* | Admin API key — bypasses tenant enforcement |
| `CAURA_API_KEY` | *(empty)* | Shared perimeter key for a network-exposed OSS deployment |
| `GATEWAY_SHARED_SECRET` | *(empty)* | Secret required in `X-Gateway-Secret` before gateway identity headers are trusted |
| `JWT_SECRET` | `change-me-in-production` | JWT signing secret; must be changed in production |
| `EMBEDDING_PROVIDER` | `openai` | `openai`, `local`, or `fake` |
| `ENTITY_EXTRACTION_PROVIDER` | `openai` | `openai`, `gemini`, `anthropic`, `openrouter`, `fake`, or `none` |
| `ENTITY_EXTRACTION_MODEL` | `gpt-5.4-nano` | LLM model for enrichment and entity extraction |
| `OPENAI_API_KEY` | — | Required for OpenAI embeddings and enrichment |
| `USE_LLM_FOR_MEMORY_CREATION` | `true` | LLM auto-classifies type, weight, title, summary, tags on write |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic |
| `OPENROUTER_API_KEY` | — | Required for OpenRouter |
| `GEMINI_API_KEY` | — | Required for Gemini (Developer API, from AI Studio) |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed CORS origins |
| `ENVIRONMENT` | `development` | `development` or `production` |
| `SETTINGS_ENCRYPTION_KEY` | — | Fernet key for encrypting tenant settings. Required in production |
| `PLATFORM_LLM_PROVIDER` | *(empty)* | Platform-default LLM: `openai`, `vertex`, or empty to disable |
| `PLATFORM_LLM_MODEL` | *(empty)* | Model override (e.g. `gpt-5.4-nano`, `gemini-3.1-flash-lite-preview`) |
| `PLATFORM_LLM_API_KEY` | — | OpenAI API key for the platform LLM singleton |
| `PLATFORM_LLM_GCP_PROJECT_ID` | — | GCP project for platform Vertex LLM |
| `PLATFORM_LLM_GCP_LOCATION` | `us-central1` | GCP region for platform Vertex LLM |
| `PLATFORM_EMBEDDING_PROVIDER` | *(empty)* | Platform-default embeddings: `openai` or empty to disable |
| `PLATFORM_EMBEDDING_MODEL` | *(empty)* | Embedding model override (e.g. `text-embedding-3-small`) |
| `PLATFORM_EMBEDDING_API_KEY` | — | OpenAI API key for platform embeddings |

With `ENVIRONMENT=production`, startup additionally requires
`ADMIN_API_KEY`, a non-default `JWT_SECRET`, `SETTINGS_ENCRYPTION_KEY`, and
either `GATEWAY_SHARED_SECRET` or `CAURA_API_KEY`. Standalone mode is rejected
in production.

## Project structure

```text
caura/
├── core-api/                      # Main FastAPI service
│   └── src/core_api/
│       ├── app.py                 # FastAPI app, lifespan, middleware
│       ├── mcp_server.py          # MCP server (Streamable HTTP, 12 tools)
│       ├── constants.py           # Limits and ranking parameters
│       ├── config.py              # Settings (env vars)
│       ├── auth.py                # API key + JWT auth, tenant enforcement
│       ├── routes/                # Route handlers
│       ├── services/              # Business logic
│       ├── providers/             # LLM/embedding abstraction + fallback
│       ├── pipeline/              # Composable write/search pipelines
│       └── tools/                 # MCP tool implementations
│
├── core-storage-api/              # PostgreSQL CRUD microservice
│   └── src/core_storage_api/
│       ├── routers/               # Memory, entity, document, fleet CRUD
│       ├── services/              # ORM operations
│       └── database/              # Engine initialization and Alembic migrations
│
├── plugin/                        # OpenClaw plugin (TypeScript)
│   └── src/
│       ├── tools.ts               # Tool implementations
│       ├── agent-auth.ts          # Per-agent credentials (agent-scoped mc_ keys)
│       ├── context-engine.ts      # Auto-read/write lifecycle
│       ├── heartbeat.ts           # 60s heartbeat → Caura API
│       └── educate.ts             # Agent education delivery
│
├── common/                        # Shared SQLAlchemy ORM models and constants
├── tests/                         # Test suite
├── scripts/                       # Smoke tests, benchmarks, export tools
├── docker-compose.yml             # Production-like stack
├── docker-compose.dev.yml         # Dev stack
└── .env.example                   # Common OSS configuration template
```

## Latency benchmarks

Typical results on a single-instance deployment (OpenAI embeddings + GPT-5.4 Nano):

| Operation | Mean | P50 | P95 |
|---|---|---|---|
| `caura_write` | ~2000ms | ~2000ms | ~2300ms |
| `caura_recall` | ~650ms | ~640ms | ~670ms |
| `caura_recall` (with `include_brief=true`) | ~1300ms | ~1200ms | ~2100ms |

Write latency is dominated by LLM enrichment. Recall latency by the embedding API call.

See [`BENCHMARKS.md`](../BENCHMARKS.md) and the
[performance guide](performance.md) for current methodology and reproducible
benchmarks.
