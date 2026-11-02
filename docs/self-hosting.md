# Self-hosting Caura

The fastest self-hosted path is Docker Compose. It starts PostgreSQL with
pgvector, Redis, the storage service, and the public REST/MCP API.

For a keyless first run, use the executable example in the
[README quick start](../README.md#quick-start). This
guide covers provider configuration, production-minded deployment details,
authentication modes, and tests.

> **Prefer not to use Docker?** See
> [Manual deployment (without Docker)](../README.md#manual-deployment-without-docker)
> for the bare-Python path.
>
> **No cloud API key or external calls?** Use the
> [local embedder](local-embedder.md) (`BAAI/bge-m3` through HuggingFace TEI).

## Prerequisites

- **Docker Engine 24+** (Linux) or **Docker Desktop** (macOS / Windows). Confirm with `docker --version`.
- **Docker Compose v2** (built into current Docker Desktop releases). Confirm with `docker compose version`.
- **Git** for cloning.
- About 2 GB free disk for images and the PostgreSQL data volume.

## 1. Clone and configure

```bash
git clone https://github.com/caura-ai/caura.git
cd caura
cp .env.example .env
```

Set your AI provider in `.env`. Minimal OpenAI configuration:

```env
EMBEDDING_PROVIDER=openai
ENTITY_EXTRACTION_PROVIDER=openai
USE_LLM_FOR_MEMORY_CREATION=true
OPENAI_API_KEY=sk-...
```

Without AI keys the stack still starts. With the default
`EMBEDDING_PROVIDER=openai` and no key (and no `PLATFORM_EMBEDDING_*`),
memories are stored **without** an embedding — keyword search still finds
them, and core-api logs one ERROR naming the missing key — so they are not
mistaken for embedded rows once a key is configured. Set
`EMBEDDING_PROVIDER=fake` to store deterministic, non-semantic test vectors
instead; that is useful for exercising the API surface but not for evaluating
semantic recall.

> **Want zero cloud API calls?** v2.0+ includes a self-hosted embedder profile
> (`BAAI/bge-m3` on a
> [HuggingFace TEI](https://github.com/huggingface/text-embeddings-inference)
> sidecar). Start it with `docker compose --profile embed-local up -d --wait`
> and set the three `OPENAI_EMBEDDING_*` values documented in `.env.example`.
> See the [local-embedder guide](local-embedder.md) for the complete setup. Combined
> with `IS_STANDALONE=true`, this makes the deployment fully self-contained.

<details>
<summary>Provider matrix</summary>

| Provider | `.env` settings | Required key |
|---|---|---|
| **OpenAI** (default) | `EMBEDDING_PROVIDER=openai`<br>`ENTITY_EXTRACTION_PROVIDER=openai` | `OPENAI_API_KEY` |
| **Google Gemini** | `EMBEDDING_PROVIDER=openai`<br>`ENTITY_EXTRACTION_PROVIDER=gemini` | `GEMINI_API_KEY` + `OPENAI_API_KEY` |
| **OpenRouter** | `EMBEDDING_PROVIDER=openai`<br>`ENTITY_EXTRACTION_PROVIDER=openrouter` | `OPENROUTER_API_KEY` + `OPENAI_API_KEY` |
| **Atlas Cloud** | `EMBEDDING_PROVIDER=openai`<br>`ENTITY_EXTRACTION_PROVIDER=atlascloud` | `ATLASCLOUD_API_KEY` + `OPENAI_API_KEY` |
| **Self-hosted (TEI / bge-m3)** | `--profile embed-local` + `OPENAI_EMBEDDING_BASE_URL=http://tei:80/v1`<br>+ `OPENAI_EMBEDDING_MODEL=BAAI/bge-m3`<br>+ `OPENAI_EMBEDDING_SEND_DIMENSIONS=false` | none — runs locally |

Gemini, OpenRouter and Atlas Cloud do not provide embedding APIs here, so pair them with
OpenAI or TEI for embeddings. `ENTITY_EXTRACTION_PROVIDER=anthropic` is not
supported: enrichment, entity extraction and contradiction detection need
structured JSON output, which Anthropic's OpenAI-compatible endpoint rejects,
so core-api refuses to start with it. Gemini uses the Google AI Studio
key-auth Developer API; it does not require a GCP project or application
default credentials. TEI keeps `EMBEDDING_PROVIDER=openai` because it exposes
an OpenAI-compatible API.

</details>

## 2. Start the stack

```bash
docker compose up -d --wait
```

> **Security requirement:** port 8002 belongs to the internal storage data
> plane and must never be exposed to the host or an untrusted network. The
> shipped Compose file does not publish it. `core-api` reaches it only on the
> private Compose network and authenticates with a random per-installation
> secret generated in a private Docker volume. Do not add an `8002:8002` port
> mapping. For local debugging, bind only `127.0.0.1:8002:8002` and keep the
> `CORE_STORAGE_SHARED_SECRET` check enabled. Only exact `GET /healthz` and
> `GET /readyz` probes are public; every storage data route requires the secret.

The first run pulls multi-architecture images from `ghcr.io` for `linux/amd64`
and `linux/arm64`. Later runs reuse the cached image. Set `CAURA_VERSION` to a
concrete v2 release tag in `.env` to pin a release. To build from a local
checkout, run:

```bash
docker compose up --build --pull never
```

To refresh an image at the same tag:

```bash
docker compose pull
docker compose up -d --wait
```

There is no silent version drift: without an explicit pull, the local cache
wins.

### Offline and air-gapped operation

- If the image is cached, `docker compose up -d --pull never` starts without a registry request.
- If no image is cached, `docker compose up --build --pull never` builds from source. The build itself still downloads apt and PyPI packages, so it needs network the first time (a warm build cache can make later rebuilds offline).
- On a host with no network at all, pull or build the images on a connected machine, `docker save` them to a file, and `docker load` it on the air-gapped host.
- For a strict no-network guarantee, add a `docker-compose.override.yml` that sets `pull_policy: never` for the three application services. Compose then fails fast when an image is absent.

### Service URLs

| Service | URL |
|---|---|
| Core API (REST + MCP) | http://localhost:8000 |
| PostgreSQL (pgvector) | localhost:5432 |
| Redis | localhost:6379 |

`core-storage-api` is intentionally reachable only by services on the Compose
network; it has no host URL.

### What the stack contains

`docker compose up` starts five long-running containers and two one-shot
containers, `storage-secret-init` and `admin-key-init`:

| Container | Role |
|---|---|
| `db` | PostgreSQL 16 + pgvector |
| `redis` | Cache and rate limiting; carries settings changes to every core-api worker at once |
| `core-storage-api` | Storage service (SQL + vector search) |
| `core-api` | REST + MCP surface; embedding and enrichment run in-process (`deployment_mode=inline`) |
| `core-operations` | Lifecycle scheduler: nightly expiry, stale archival, purge, crystallization, entity linking and insights, plus hourly reconciliation (02:00 UTC by default) |

`core-operations` calls core-api's admin-only endpoints. When `ADMIN_API_KEY`
is blank, `admin-key-init` generates a key into a private volume that only
`core-api` and `core-operations` mount, so the schedule works on a bare
`docker compose up`. An `ADMIN_API_KEY` set in `.env` replaces it for both. To
stop the schedule, stop the `core-operations` service or set
`IS_STANDALONE: "true"` in its `environment`; nothing expires, archives or is
purged without it.

The optional `tei` service starts only with `--profile embed-local`.
`core-worker`, platform-tier services, and the Google Pub/Sub event bus are
managed/enterprise components. The OSS stack uses the in-process event bus and
performs embedding and enrichment inside `core-api`, so it needs no worker.

## 3. Verify

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","storage":"connected","redis":"connected","event_bus":"ok"}
```

## 4. Write and search

```bash
# Write a memory (standalone mode — no API key needed)
curl -X POST http://localhost:8000/api/v1/memories \
  -H "X-API-Key: standalone" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "agent_id": "quickstart", "content": "Our auth service uses JWT with 15-minute expiry."}'

# Search for it semantically after configuring an embedding provider
curl -X POST http://localhost:8000/api/v1/search \
  -H "X-API-Key: standalone" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "query": "authentication token lifetime"}'
```

`agent_id` names the writer, so every memory carries provenance. It is optional
for a standalone deployment in current source and defaults to `mcp-agent`, but
published v2.x images still require it. Passing it explicitly works everywhere.

Write bodies reject undeclared fields with a `422` response and name them in
`error.details.unknown_fields`. Put caller-owned values under `metadata`.
Search and filter bodies deliberately ignore unknown fields. See
[API surface contracts](api-surfaces.md#request-body-contract-writes-are-strict-searches-are-not).

The write response contains inferred `memory_type`, `title`, `status`, and
`weight`. `summary` and `tags` live under `metadata`. On a deferred fast-write
deployment, enrichment is asynchronous and the immediate response may contain
`metadata.enrichment_pending: true`.

Semantic embedding can also be asynchronous. While
`metadata.embedding_pending: true`, the memory is available through keyword
search and `GET /memories`, but does not compete on semantic similarity. If a
caller must immediately search what it wrote, pass `write_mode: "strong"` to
embed inline. This adds a provider call to the write request, so choose it per
write rather than globally.

`POST /search` returns matches in an `items` array. Each item contains the full
memory plus its `similarity` score.

## Upgrading

Pull the new images and run `docker compose up -d`. `core-storage-api` applies
any pending schema migrations when it starts, before it serves traffic. If a
migration is interrupted (the container is killed or the database drops the
connection), restart the service. Each migration either rolls back completely
or is written to be retried, so the restart runs it again from the start.

Restore a database with a full `pg_dump`, which includes the `alembic_version`
table that records the schema revision. If that table is missing (a dump of
selected tables, or one dropped by hand), the service records the current
revision only when the schema shows the newest migration was applied. Otherwise
it refuses to start, because it cannot tell how many migrations still need to
run. Find the revision the database was really at (from the `alembic_version`
of the database it was copied from, or the release it last ran), record it,
and start the service. It then applies the remaining migrations. From a source
checkout, run `alembic stamp <revision>` from the repository root. With the
compose stack, write the same row directly:

```bash
docker compose exec db psql -U caura -d caura -c \
  "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
   INSERT INTO alembic_version VALUES ('<revision>');"
```

## Authentication modes

Choose one mode in `.env`, then restart with `docker compose up -d`.

### Standalone

Single-tenant (`tenant_id="default"`), intended for local or personal installs:

```env
IS_STANDALONE=true
```

REST and MCP need no API key in standalone mode. Pair this mode with the
[local embedder](local-embedder.md) for a fully self-contained deployment.

### Admin key

Multi-tenant with full access:

```env
ADMIN_API_KEY=your-long-random-admin-key
```

Send the key in `X-API-Key` and include `tenant_id` in request bodies or query
parameters.

### Shared gate

For network-exposed OSS deployments:

```env
CAURA_API_KEY=your-shared-key
```

Clients send `X-API-Key: your-shared-key` and `X-Tenant-ID: <tenant>`.

See the [agent installation guide](../AGENT-INSTALL.md) for the complete
self-install flow.

## Running tests

```bash
# Root unit tests (no database needed)
pytest tests/ -m "unit"

# Root integration suite (requires PostgreSQL)
docker compose up -d db
pytest tests/ -m "not benchmark"

# Smoke test against a live API (~30s, auto-cleanup)
python scripts/smoke_test.py --url http://localhost:8000 --api-key <admin-key>
```

CI also runs service-, worker-, operations-, client-, and plugin-specific
suites from their respective directories.

### Standalone HTTP authority

Standalone REST and MCP accept loopback Host/Origin values by default. The
shipped development environment also allows `core-api:8000` for sibling containers. For a
reverse proxy, set `PUBLIC_API_URL` to its externally visible API URL. Additional
exact authorities and origins can be supplied as comma-separated
`STANDALONE_ALLOWED_HOSTS` and `STANDALONE_ALLOWED_ORIGINS`; an authority may use
`:*` to allow any port. Do not add untrusted domains to either list.

Explicit `CORS_ORIGINS` entries are also accepted as Origin values, so a
configured browser frontend passes both CORS and the standalone boundary.
The wildcard `*` does not expand this boundary, and CORS origins do not add
allowed Host values.

The stock and development Compose APIs bind to `127.0.0.1` by default. To expose
an API to a network, configure `API_BIND=0.0.0.0`, `CAURA_API_KEY`, and
`PUBLIC_API_URL` together. Keep the key private and terminate TLS at the reverse
proxy.

### PostgreSQL transport and migration roles

With `ENVIRONMENT=production` or `sandbox`, storage defaults to
`POSTGRES_REQUIRE_SSL=true` and verifies the database certificate and hostname.
Mount the database CA certificate and set `POSTGRES_SSL_ROOT_CERT` to its path
when it is not in the system trust store. The database URL must use a hostname
covered by that certificate. Apply this configuration to both writer and reader.

`POSTGRES_SSL_MODE=verify-full` also enables verification in development.
`verify-ca` verifies the certificate without checking the hostname; `require`
requires encryption without certificate verification. A stronger mode already
specified by the URL is preserved. Local Compose explicitly disables the TLS
requirement for its bridge-network database; deployments can explicitly opt out
with `POSTGRES_REQUIRE_SSL=false` when necessary during migration to TLS.
An explicit `POSTGRES_SSL_MODE` independently requests its policy and takes
precedence over that flag; clear the mode too when opting out of both settings.
A stronger URL policy is always preserved.

Set `MIGRATION_DATABASE_URL` to a dedicated schema-owner credential. Boot-time
Alembic and the Alembic CLI use that credential, while request and read pools
continue using `DATABASE_URL` and `READ_DATABASE_URL`. The migration pool closes
when startup finishes, including failed startup. Reader-role services never open
it. With no migration URL, the existing single-credential setup is retained for
compatibility; configuring the two roles is an operator step.

For a new database, provision separate `caura_migrator` and `caura_runtime` login
roles through your database administration tooling, with independent passwords.
Give the migrator ownership of the application schema and migration objects.
Keep runtime `NOSUPERUSER NOCREATEDB NOCREATEROLE`, without membership in the
migrator role. Run migrations as the migrator, then grant runtime only the
application privileges (the following example uses the OSS `public` schema):

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO caura_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO caura_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO caura_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE caura_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO caura_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE caura_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO caura_runtime;
REVOKE ALL ON TABLE public.alembic_version FROM caura_runtime;
```

On an existing database, review current object ownership and transfer the
application objects to the migrator before switching the URLs. Verify the runtime
role can perform application DML but cannot create, alter, or drop a table or
update `alembic_version`. Review these grants when adding migrations; these
instructions do not change database users or privileges automatically.
