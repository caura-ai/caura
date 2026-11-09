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
| Keystones | `GET /keystones`, `POST /keystones`, `DELETE /keystones/{doc_id}` (supported legacy route: `/memclaw/keystones`) | <!-- legacy-name-floor: documents the compatibility route -->
| Fleet | `POST /fleet/heartbeat`, `GET /fleet/nodes`, `POST /fleet/commands`, `GET /fleet/commands` |
| Agents | `GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}/trust`, `POST /admin/agent-keys/provision` (managed platform only: atomic key + row + trust + fleet; a self-hosted core-api has no such route), `GET /whoami` (identity probe) |
| Insights | `POST /insights/generate` |
| Evolve | `POST /evolve/report` |
| Crystallizer | `POST /crystallize`, `POST /crystallize/all`, `GET /crystallize/reports`, `GET /crystallize/latest` |
| Settings | `GET/PUT /settings` |
| System | `GET /health`, `GET /version`, `GET /tool-descriptions`, `GET /audit-log`, `GET /telemetry`, `POST /telemetry/rotate` (see [telemetry.md](telemetry.md)) |
| MCP | `POST /mcp` (Streamable HTTP transport, mounted at app root) |
| Bootstrap (plugin) | `GET /plugin-source`, `GET /plugin-source-hash`, `GET /plugin-manifest`, `GET/POST /install-plugin`, `GET /install-skill[?skill=memclaw\|company-brain]`, `GET /skill/{memclaw\|company-brain}`. `/plugin-source`, `/plugin-manifest`, and `GET/POST /install-plugin` are also aliased under `/api` (no `/v1`) for the generated installer. | <!-- legacy-name-floor: published skill query parameter and route -->

### Plugin environment variables

Read by the OpenClaw plugin. The plugin's published name (`memclaw`) and these variables are the public contract; the plugin's TypeScript module structure is internal. <!-- legacy-name-floor: documents the frozen published plugin id -->

| Var | Purpose |
|---|---|
| `CAURA_API_URL` | Final base URL of the core-api server. Credential-bearing plugin requests reject redirects; configure the destination directly. Use `https://` for any non-loopback host — see `CAURA_ALLOW_INSECURE_HTTP`. |
| `CAURA_API_KEY` | Tenant or admin API key sent in `X-API-Key`. |
| `CAURA_ALLOW_INSECURE_HTTP` | `true` to send `CAURA_API_KEY` over plain `http://` to a non-loopback host (e.g. a trusted private network). Unset (default): the plugin refuses such calls with an error naming the host. Loopback (`localhost`, `127.0.0.0/8`, `::1`) never needs it. Plain HTTP to a non-loopback host also carries fleet commands, plugin source and the skills catalog, so there the plugin rejects `deploy`, `update_plugin` and `educate` commands and does not sync skills (installed skills are kept), with or without this opt-in and with or without `CAURA_API_KEY`: anyone on the path could otherwise push code or agent instructions to the node. |
| `CAURA_TENANT_ID` | Optional pre-resolved tenant id; bypasses lookup. |
| `CAURA_FLEET_ID` | Default fleet id for writes/heartbeat. |
| `CAURA_NODE_NAME` | Fleet node identifier reported on heartbeat. |
| `CAURA_AUTO_WRITE_TURNS` | Automatic conversation-memory writes (default `true`), limited to explicit per-call direct-conversation keys. Group/unknown contexts are refused. The flush callback supplies no conversation identity, so its automatic turn is disabled. Set `false` (or `0`, `no`, `off`, any case; blank is off) to disable capture. Plugin config overrides the environment. Heartbeats report the effective boolean as `metadata.auto_capture_enabled`. |

Automatic writes require a per-call `sessionKey` in OpenClaw's explicit
`agent:<name>:<channel>:dm:<id>` or `agent:<name>:<channel>:direct:<id>` shape.
A factory default, opaque `sessionId`, group/channel key or unknown shape does
not establish a direct conversation. This deliberately skips capture for hosts
that cannot provide that context; it does not guess from message content.

Captured episodes remain `scope_agent`. Current servers recognize the capture
tags, mask credentials and personal data even with the organization's PII gate
off, and stamp protected `system_metadata.capture`. Automatic recall returns a
captured row only to its originating conversation. Authorized organization
members who may read all scopes can still read agent-private memories.

Legacy flat `ingest` calls can persist substantive user messages in direct
contexts, capped at ten writes per session; real wrapper-shaped OpenClaw ingest
remains non-persisting. Assistant summaries allow ten attempted writes per
session, including failures and concurrent hooks. These budgets survive engine
recreation, track at most 100 conversations per process, and refuse new
conversations once full until restart. User messages and assistant summaries
require at least 100 characters and are truncated to 500 plus an ellipsis.
Compaction summaries require direct context too. The automatic pre-compaction
flush is disabled because its supported callback cannot identify a conversation.

Turning the switch off leaves local buffering, recall, deliberate memory tools
and ordinary runtime compaction available. Existing memories are not deleted.
The independently enabled Interviewer retains its own controls, but message
mirroring shares the direct-conversation gate. Heartbeat state describes the
parsed global switch, not whether an individual conversation is eligible.
Restart the plugin after changing its environment. This documentation is
operator guidance; the checked host integration establishes no end-user notice
mechanism, so documentation is not claimed as capture notice.

**Legacy spellings.** Every `CAURA_*` variable in this document — the table above, the `CAURA_API_KEY` server gate, and `CAURA_VERSION` in compose — currently also answers to its pre-rename `MEMCLAW_*` name. Use `CAURA_*` for new configuration. Where both are set the first **non-empty** value wins — deliberately, rather than the first one *defined* — so an unfilled `CAURA_FOO=` in a deploy template cannot blank out a working `MEMCLAW_FOO`. <!-- legacy-name-floor: documents the current dual-read behavior -->

New installs are written with the `CAURA_*` names. Variables without the prefix
(`ADMIN_API_KEY`, `POSTGRES_*`, `IS_STANDALONE`, …) never had a branded spelling.

### Server environment variables

These mirror the [configuration reference](api-reference.md#configuration).

| Group | Vars |
|---|---|
| Database | Storage service: `DATABASE_URL`, `READ_DATABASE_URL` or a complete `ALLOYDB_*` connection set; migration/dev helpers: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (stock Compose hardcodes its container connection values) |
| Auth | `ADMIN_API_KEY`, `ADMIN_API_KEY_FILE`, `CAURA_API_KEY`, `GATEWAY_SHARED_SECRET`, `CORE_STORAGE_SHARED_SECRET`, `CORE_STORAGE_SHARED_SECRET_FILE`, `IS_STANDALONE` |
| Providers | `EMBEDDING_PROVIDER`, `ENTITY_EXTRACTION_PROVIDER`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `USE_LLM_FOR_MEMORY_CREATION` |
| Runtime | `CORS_ORIGINS`, `ENVIRONMENT`, `SETTINGS_ENCRYPTION_KEY`, `REDIS_URL` |

Production startup requires `ADMIN_API_KEY` (or `ADMIN_API_KEY_FILE`),
`SETTINGS_ENCRYPTION_KEY`, and either `GATEWAY_SHARED_SECRET` or
`CAURA_API_KEY`; `IS_STANDALONE=true` is not allowed. `JWT_SECRET` is no longer
read, so a value left set is ignored.

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
- Gateway-injected HTTP headers (`X-Gateway-Secret`, `X-Tenant-ID`, `X-Agent-ID`, `X-Org-Read-Only`, `X-User-ID`)
- Most `/api/v1/admin/*` and all `/api/v1/testing/*` routes (the documented exception is `POST /admin/agent-keys/provision`, part of the managed platform's stable identity-bootstrap surface — see the Agents row above)
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
