# Public API and stability

Caura follows [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html). The surfaces
below are stable; everything else is internal and may change in any release.

For exact REST request and response shapes, use the OpenAPI schema served at
`/api/openapi.json`. See the [API reference](api-reference.md) for a curated map.

## Stable surfaces

### MCP tools (12)

The MCP server is mounted at `/mcp`. Tool names, parameter names, and the documented op-dispatch values are stable.

| Tool | Purpose |
|---|---|
| `caura_recall` | Hybrid semantic + keyword search over memories, with optional LLM-summarised brief. |
| `caura_write` | Single or batch (≤100) memory write; configured enrichment providers can infer type, title, summary, and tags. |
| `caura_manage` | Per-memory lifecycle, op-dispatched: `read` \| `update` \| `transition` \| `delete` \| `bulk_delete` \| `lineage`. |
| `caura_list` | Non-semantic enumeration with filters, sort, cursor pagination. |
| `caura_doc` | Structured-document CRUD, op-dispatched: `write` \| `read` \| `query` \| `delete` \| `list_collections` \| `search`. |
| `caura_entity_get` | Look up a knowledge-graph entity by UUID. |
| `caura_tune` | Read/update an agent's per-search profile (top_k, fts_weight, freshness, blend, …). |
| `caura_insights` | Karpathy-Loop reflection: contradictions, failures, stale, divergence, patterns, discover. |
| `caura_evolve` | Karpathy-Loop feedback: record an outcome (`success` \| `failure` \| `partial`) against memories. |
| `caura_stats` | Aggregate counts: total + breakdowns by `type` / `agent` / `status`. Read-only. |
| `caura_keystones` | Read mandatory governance rules for the current scope (tenant + fleet + agent merged). Call once per session. |
| `caura_keystones_set` | Author/remove keystone rules, op-dispatched: `set` \| `delete`. Trust ≥ 1 for self-authored `scope=agent` (requires an explicit `agent_id` equal to the caller); ≥ 2 otherwise, including `scope=agent` with `agent_id` omitted. |

> Skill sharing uses the generic `caura_doc` surface — write/read/query/search/delete on `collection="skills"`. The server validates the slug and embeds `data["summary"]` for semantic discovery (with a back-compat fallback to `data["description"]` for skills).

### REST endpoints

All paths are prefixed with `/api/v1` unless noted. Request and response shapes documented in the OpenAPI schema at `/api/openapi.json` are part of the contract.

| Area | Endpoints |
|---|---|
| Memory | `GET/POST /memories`, `PATCH /memories/{id}`, `DELETE /memories/{id}`, `PATCH /memories/{id}/status`, `POST /memories/bulk`, `POST /memories/bulk-delete`, `GET /memories/stats`, `GET /memories/{id}`, `GET /memories/{id}/contradictions`, `POST /search`, `POST /recall`, `POST /ingest/preview`, `POST /ingest/commit` |
| Knowledge graph | `GET /entities`, `GET /entities/{id}`, `POST /entities/upsert`, `GET /graph`, `POST /relations/upsert` |
| Documents | `POST /documents`, `GET /documents`, `GET /documents/{id}`, `POST /documents/query`, `DELETE /documents/{id}` |
| Keystones | `GET /keystones`, `POST /keystones`, `DELETE /keystones/{doc_id}` (permanent legacy alias: `/memclaw/keystones`) | <!-- legacy-name-ok: taught as legacy alias -->
| Fleet | `POST /fleet/heartbeat`, `GET /fleet/nodes`, `POST /fleet/commands`, `GET /fleet/commands` |
| Agents | `GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}/trust`, `POST /admin/agent-keys/provision` (atomic key + row + trust + fleet), `GET /whoami` (identity probe) |
| Insights | `POST /insights/generate` |
| Evolve | `POST /evolve/report` |
| Crystallizer | `POST /crystallize`, `POST /crystallize/all`, `GET /crystallize/reports`, `GET /crystallize/latest` |
| Settings | `GET/PUT /settings` |
| System | `GET /health`, `GET /version`, `GET /tool-descriptions`, `GET /audit-log` |
| MCP | `POST /mcp` (Streamable HTTP transport, mounted at app root) |
| Bootstrap (plugin) | `GET /plugin-source`, `GET /plugin-source-hash`, `GET /plugin-manifest`, `GET/POST /install-plugin`, `GET /install-skill[?skill=memclaw\|company-brain]`, `GET /skill/{memclaw\|company-brain}`. `/plugin-source`, `/plugin-manifest`, and `GET/POST /install-plugin` are also aliased under `/api` (no `/v1`) for the generated installer. | <!-- legacy-name-floor: published skill query parameter and route -->

### Plugin environment variables

Read by the OpenClaw plugin. The plugin's published name (`memclaw`) and these variables are the public contract; the plugin's TypeScript module structure is internal.

| Var | Purpose |
|---|---|
| `CAURA_API_URL` | Base URL of the core-api server. |
| `CAURA_API_KEY` | Tenant or admin API key sent in `X-API-Key`. |
| `CAURA_TENANT_ID` | Optional pre-resolved tenant id; bypasses lookup. |
| `CAURA_FLEET_ID` | Default fleet id for writes/heartbeat. |
| `CAURA_NODE_NAME` | Fleet node identifier reported on heartbeat. |
| `CAURA_AUTO_WRITE_TURNS` | Auto-write turn summaries (default `true`). |

**Legacy spellings.** Every `CAURA_*` variable in this document — the table above, the `CAURA_API_KEY` server gate, and `CAURA_VERSION` in compose — also answers to its pre-rename `MEMCLAW_*` name and will keep doing so: swap the prefix, and the rest of the name is unchanged (`CAURA_API_URL` ⇄ `MEMCLAW_API_URL`). Where both are set the first **non-empty** value wins — deliberately, rather than the first one *defined* — so an unfilled `CAURA_FOO=` in a deploy template cannot blank out a working `MEMCLAW_FOO`. <!-- legacy-name-ok: rule 3 dual-read alias — the one surviving alias table -->

New installs are written with the `CAURA_*` names. Variables without the prefix
(`ADMIN_API_KEY`, `POSTGRES_*`, `IS_STANDALONE`, …) never had a branded spelling.

### Server environment variables

These mirror the [configuration reference](api-reference.md#configuration).

| Group | Vars |
|---|---|
| Database | Storage service: `DATABASE_URL`, `READ_DATABASE_URL` or a complete `ALLOYDB_*` connection set; migration/dev helpers: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (stock Compose hardcodes its container connection values) |
| Auth | `ADMIN_API_KEY`, `CAURA_API_KEY`, `GATEWAY_SHARED_SECRET`, `JWT_SECRET`, `CORE_STORAGE_SHARED_SECRET`, `CORE_STORAGE_SHARED_SECRET_FILE`, `IS_STANDALONE` |
| Providers | `EMBEDDING_PROVIDER`, `ENTITY_EXTRACTION_PROVIDER`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `USE_LLM_FOR_MEMORY_CREATION` |
| Runtime | `CORS_ORIGINS`, `ENVIRONMENT`, `SETTINGS_ENCRYPTION_KEY`, `REDIS_URL` |

Production startup requires `ADMIN_API_KEY`, a non-default `JWT_SECRET`,
`SETTINGS_ENCRYPTION_KEY`, and either `GATEWAY_SHARED_SECRET` or
`CAURA_API_KEY`; `IS_STANDALONE=true` is not allowed.

### Auth modes

| Mode | Activated by | Use case |
|---|---|---|
| Standalone | `IS_STANDALONE=true` | Single-tenant self-host; auth bypassed. |
| Multi-tenant admin | `ADMIN_API_KEY=…` | Operator key for multi-tenant deployments. |
| Shared gate | `CAURA_API_KEY=…` | Optional shared secret required on authenticated non-admin routes. |

See [AGENT-INSTALL.md](../AGENT-INSTALL.md) for installation flows that exercise each mode.

## Internal (not covered by SemVer)

Anything not listed above is internal and may change in any release without a major version bump:

- Python module layout (`core_api.middleware.*`, `core_api.providers.*`, `core_api.pipeline.*`, `core_api.services.*`, `common/*`)
- Database schema, table names, migration paths
- Gateway-injected HTTP headers (`X-Gateway-Secret`, `X-Tenant-ID`, `X-Agent-ID`, `X-Org-Read-Only`)
- Most `/api/v1/admin/*` and all `/api/v1/testing/*` routes (the documented exception is `POST /admin/agent-keys/provision`, which is part of the stable identity-bootstrap surface — see the Agents row above)
- The `core-storage-api` microservice (internal, not user-facing)
- The plugin's TypeScript module structure
- API-key prefix formats — currently unified on `mc_…` (with legacy `mca_…` / `mci_…` aliases still accepted via back-compat); formats may continue to evolve

## Reporting breaking changes

Contributors who introduce a breaking change to a stable surface must:

- Add a `BREAKING CHANGE:` trailer to the commit message describing the impact and any migration steps.
- Apply the `kind/breaking` label to the pull request.

Reviewers will block changes to a stable surface without these markers. If you
are unsure whether a change is breaking, open the PR with the label and let
review decide — better to over-label than ship a silent break.
