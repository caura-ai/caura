# MemClaw — OpenClaw Integration Guide

---

> **For server setup, configuration, endpoints, Web UI, deployment, and smoke tests, see the [README](../README.md).**
> This guide covers only MCP client setup, OpenClaw plugin installation, agent trust levels, agent prompts, and usage examples.

## 1. Overview

MemClaw is a shared memory layer for OpenClaw agents. It runs as a separate API service that agents access through an OpenClaw plugin or any MCP client.

### Architecture

```
MCP Client      → Streamable HTTP  → MemClaw API (/mcp) → Postgres + pgvector
OpenClaw Agent  → tool call        → MemClaw Plugin → HTTP → MemClaw API → Postgres + pgvector
Plugin          → heartbeat (60s)  → MemClaw API ← commands (response)
Browser (UI)    → HTTP             → MemClaw API → Postgres + pgvector
```

### Components

| Component | Where it runs | What it does |
|---|---|---|
| MemClaw API | Any host (Docker, VM, cloud run, local) | FastAPI service — memories, entities, search, enrichment |
| MCP Server | Same process (`/mcp`) | Streamable HTTP endpoint for any MCP client |
| Postgres + pgvector | Anywhere (Docker, VM, managed) | Vector + relational store |
| MemClaw Plugin | OpenClaw gateway VM | Thin adapter forwarding tool calls to the API |
| Web UI | Served at `/ui` | Manage, Prism (with Graph button), Playground, Fleet, MCP Test, Ingest, Admin Dashboard |

### Tools available to agents

Tool descriptions are derived from the tool registry (`core-api/src/core_api/tools/_registry.py`) and served at `GET /api/tool-descriptions`. Both MCP and OpenClaw plugin read from this canonical source.

| Tool | MCP | OpenClaw | Purpose |
|---|---|---|---|
| `memclaw_write` | Yes | Yes | Single or batch write. Send `content` for one memory, or `items` (≤100) for a batch — the batch path batches embeddings and parallelizes enrichment. LLM auto-infers type, weight, status, title, summary, tags, temporal dates, PII flags. Contradiction detection auto-marks conflicting memories. `visibility` = `scope_agent` / `scope_team` (default) / `scope_org`. Content >2,000 chars is auto-chunked |
| `memclaw_recall` | Yes | Yes | Hybrid semantic + keyword search with graph-enhanced retrieval (expands through entity relations up to 2 hops). `include_brief=true` returns an LLM-summarized context paragraph instead of raw results. Supports `fleet_ids` for multi-fleet queries. Respects visibility. Default `top_k=5`, max 20 |
| `memclaw_manage` | Yes | Yes | Per-memory lifecycle, op-dispatched. `op=read` returns the memory; `op=update` patches fields (re-embeds if content changes); `op=transition` sets status; `op=delete` soft-deletes. Trust-enforced |
| `memclaw_list` | Yes | Yes | Non-semantic enumeration — filter by type/status/agent/weight/date, sort by `created_at`/`weight`/`recall_count`, cursor-paginate. Trust ≥ 2. Trust 3 unlocks `include_deleted` |
| `memclaw_doc` | Yes | Yes | Document CRUD, op-dispatched. `op=write` upserts a JSON doc in a named collection; `op=read` fetches by `doc_id`; `op=query` filters by field equality with ordering and pagination; `op=delete` removes by `doc_id`. Use for customer records, config, inventory — anything needing exact-field lookups |
| `memclaw_entity_get` | Yes | Yes | Look up an entity with linked memories and relations |
| `memclaw_tune` | Yes | Yes | Tune per-agent retrieval parameters (top_k, min_similarity, fts_weight, freshness, recall boost, graph hops, similarity blend) |
| `memclaw_insights` | Yes | Yes | Analyze the memory store. `focus`: `contradictions`, `failures`, `stale`, `divergence`, `patterns`, `discover`. `scope`: `agent`, `fleet`, `all`. Findings persist as `insight`-type memories (Karpathy Loop reflection step) |
| `memclaw_evolve` | Yes | Yes | Record a real-world outcome (`success` / `failure` / `partial`) against recalled memories — adjusts weights, auto-generates preventive rules on failure (Karpathy Loop feedback edge) |
| `memclaw_stats` | Yes | Yes | Aggregate counts of memories: total + breakdowns by `type`, `agent`, `status`. Read-only — useful for dashboards (REST) and agent self-introspection (MCP) |
| `memclaw_share_skill` | Yes | Yes | Share a `SKILL.md` artifact with the fleet. Default publishes to the catalog (semantic-searchable via `GET /skills?query=`); `install_on_fleet=true` also queues `install_skill` fleet commands so every node materialises the skill locally for OpenClaw discovery |
| `memclaw_unshare_skill` | Yes | Yes | Remove a shared skill. Default removes from the catalog only; `unshare_from_fleet=true` also queues `uninstall_skill` per fleet node so plugins delete the local `SKILL.md` |

- **MCP (12 tools):** Full surface. Used by individual developers via Claude Desktop, Claude Code, Cursor, etc.
- **OpenClaw plugin (12 tools):** Same set. Claims the exclusive `memory` slot, replacing `memory-core`. Includes ContextEngine lifecycle, heartbeat, and auto-education.

---

## 2. MCP Integration (Claude Desktop, Claude Code, Cursor, etc.)

MemClaw includes a built-in MCP server at `/mcp` using Streamable HTTP transport. Any MCP-compatible client connects with just a URL and an API key — no plugin install, no local server.

### Setup

Add this to your MCP client configuration:

```json
{
  "mcpServers": {
    "memclaw": {
      "url": "https://your-memclaw-instance.example.com/mcp",
      "headers": {
        "X-API-Key": "mc_your_api_key_here"
      }
    }
  }
}
```

**Config file locations:**

| Client | Config file |
|---|---|
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Claude Code | `~/.claude.json` (user scope) — preferred; register via `claude mcp add --scope user --transport http memclaw https://your-memclaw-instance.example.com/mcp --header "X-API-Key: mc_your_key"` |
| Cursor | Settings -> MCP Servers -> Add Server (type: `sse`, URL: `https://your-memclaw-instance.example.com/mcp`) |

> The Claude Code MCP-server registry lives in `~/.claude.json` — NOT `~/.claude/settings.json`. The latter's schema rejects an `mcpServers` block. Prefer the `claude mcp add` CLI over hand-editing so the correct file is written.

### Install the usage skill (Claude Code, Codex)

The MCP connection only exposes the raw tool surface. The *usage skill* —
the teachable guide that explains when to reach for memory vs doc, how
the two search strategies differ, what `embed_field` does, the trust
table, and the "recall-before-you-start / write-when-something-matters
/ supersede-don't-delete" rules — ships as a separate file that your
agent reads on-demand. Install it after the MCP config above:

```bash
# Installs SKILL.md into ~/.claude/skills/memclaw/ (Claude Code)
# and/or ~/.agents/skills/memclaw/ (Codex).
curl -s "https://your-memclaw-instance.example.com/api/v1/install-skill" \
  -H "X-API-Key: mc_your_key" | bash
```

Options:

| Query param | Effect |
|---|---|
| (none) | Install for both Claude Code and Codex |
| `?agent=claude-code` | Only Claude Code |
| `?agent=codex` | Only Codex |

Restart your agent after installing — skills are loaded at startup.

Why this matters: without the skill, an agent can discover the 9 tool
names and their arg schemas via MCP `tools/list`, but it has no
mental model for the two-store design (memory vs doc), the trust
levels, or which op to reach for in an ambiguous situation. With the
skill installed, all of that is in the agent's context on-demand. A
brand-new agent that connected via `claude mcp add` without also
running this installer will still work, but will hit the same
"tools present, guidance missing" gap that made us write the skill
in the first place.

### Available tools

The MCP server exposes 12 tools that clients discover automatically. Descriptions are canonical — served from `GET /api/tool-descriptions`, derived from the tool registry (`core-api/src/core_api/tools/_registry.py`).

| Tool | Purpose |
|---|---|
| `memclaw_write` | Store a memory. Single write (`content`) or batch (`items` ≤100). LLM auto-infers type, title, summary, tags, embedding. Long content auto-chunked |
| `memclaw_recall` | Hybrid semantic + keyword search with graph-enhanced retrieval. `include_brief=true` returns an LLM-summarized context paragraph. Supports `fleet_ids` |
| `memclaw_manage` | Per-memory lifecycle, op-dispatched: `read`, `update`, `transition`, `delete`, `bulk_delete`, `lineage`. Re-embeds on content updates |
| `memclaw_list` | Non-semantic enumeration — filter by type/status/agent/weight/date, sort, cursor-paginate (trust ≥ 2) |
| `memclaw_doc` | Document CRUD, op-dispatched: `write`, `read`, `query`, `delete`, `list_collections`, `search` (semantic) on named JSON collections |
| `memclaw_entity_get` | Look up an entity by UUID — returns linked memories and relationships |
| `memclaw_tune` | Tune per-agent retrieval parameters (top_k, min_similarity, fts_weight, freshness, recall boost, graph hops, similarity blend) |
| `memclaw_insights` | Analyze the store. Focus: `contradictions`, `failures`, `stale`, `divergence`, `patterns`, `discover`. Persists findings as `insight` memories |
| `memclaw_evolve` | Report an outcome (success/failure/partial) against recalled memories — adjusts weights, generates preventive rules on failure |
| `memclaw_stats` | Aggregate counts: total + breakdowns by `type`, `agent`, `status`. Read-only |
| `memclaw_share_skill` | Share a `SKILL.md` with the fleet. Default publishes to the catalog (semantic-searchable); `install_on_fleet=true` also auto-installs on every fleet node |
| `memclaw_unshare_skill` | Remove a shared skill. Default removes from catalog only; `unshare_from_fleet=true` also rms the local `SKILL.md` on fleet nodes |

### Auth

MCP uses the same tenant-scoped API keys as the REST API. The `X-API-Key` header is sent with every request.

- Tenant-scoped keys: can only access their tenant's memories
- Admin keys: rejected (MCP requires tenant-scoped keys for data isolation)
- Demo keys: read-only (search and entity lookup only)

### Example usage

Once configured, the MCP client handles tool discovery. Agents can use MemClaw tools naturally:

> "Search my memories for anything about the Postgres migration"
> -> calls `memclaw_recall` with query "Postgres migration"

> "Remember that we decided to use pgvector for embeddings instead of Pinecone"
> -> calls `memclaw_write` with that content; LLM auto-classifies as `decision` type

> "Mark that migration task as confirmed"
> -> calls `memclaw_manage` with `op="transition"` and `status="confirmed"`

### MCP vs OpenClaw plugin

| | MCP | OpenClaw Plugin |
|---|---|---|
| Setup | Add URL + key to config | Install plugin on gateway VM |
| Works with | Any MCP client | OpenClaw agents only |
| Tools | 9 (write, recall, manage, list, doc, entity_get, tune, insights, evolve) | 9 (same) |
| RDF triples | Not exposed (contradiction detection via semantic similarity only) | Yes — `subject_entity_id`, `predicate`, `object_value` on write |
| Temporal filter | Not exposed | Yes — `valid_at` on search |
| Visibility | Passed per-call (`scope_agent` / `scope_team` / `scope_org`) | Passed per-call (`scope_agent` / `scope_team` / `scope_org`) |
| Multi-fleet search | Yes — `fleet_ids` parameter | Yes — `fleet_ids` parameter |
| Fleet ID | Passed per-call (optional) | Auto-stamped from gateway env |
| Best for | Individual developers, Claude Desktop/Code users | OpenClaw fleet deployments |

---

## 3. OpenClaw Plugin Installation

The plugin is a TypeScript package in the `plugin/` directory of this repo. It claims the exclusive `memory` slot on an OpenClaw gateway, replacing the built-in `memory-core`, and provides 9 agent-facing tools, a ContextEngine with auto-read/write lifecycle, a heartbeat loop, and agent auto-education.

### Build from source

On a machine with `node` (v18+) and `npm`:

```bash
git clone https://github.com/caura-ai/caura-memclaw.git
cd caura-memclaw/plugin
npm install
npm run build            # emits plugin/dist/
```

### Install on an OpenClaw gateway

```bash
# On the gateway machine
mkdir -p ~/.openclaw/plugins/memclaw
# Copy the built plugin from your build machine (or rebuild here):
scp -r plugin/dist plugin/package.json plugin/openclaw.plugin.json \
    user@gateway:~/.openclaw/plugins/memclaw/
```

### Environment variables

Add to `~/.openclaw/plugins/memclaw/.env`:

```bash
MEMCLAW_API_URL=https://your-memclaw-instance.example.com   # your MemClaw API
MEMCLAW_API_KEY=mc_your_key_here                             # tenant-scoped API key
MEMCLAW_FLEET_ID=fleet-001                                   # identifies this fleet
MEMCLAW_NODE_NAME=my-gateway                                 # friendly name shown in Fleet page
# MEMCLAW_TENANT_ID=                                         # auto-resolved from API key
# MEMCLAW_AUTO_WRITE_TURNS=true                              # default; set false to disable auto-write
# MEMCLAW_AUTO_FIX_CONFIG=false                              # set true to auto-fix openclaw.json on startup
```

The plugin loads this `.env` file automatically (only `MEMCLAW_*` keys are read). If you use systemd, also add the vars to a drop-in file (`.env` values don't override existing process env).

**Configure OpenClaw** — edit `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "allow": ["memclaw"],
    "entries": {
      "memclaw": { "enabled": true, "config": {} },
      "memory-core": { "enabled": false }
    },
    "slots": {
      "memory": "memclaw"
    },
    "load": { "paths": ["/home/openclaw/.openclaw/plugins/memclaw"] }
  },
  "tools": {
    "alsoAllow": [
      "memclaw_write", "memclaw_recall", "memclaw_manage",
      "memclaw_list", "memclaw_doc", "memclaw_entity_get",
      "memclaw_tune", "memclaw_insights", "memclaw_evolve"
    ]
  }
}
```

**Critical:** The `plugins.slots.memory` and `memory-core` disablement are required. OpenClaw only loads one `kind: "memory"` plugin at a time — without switching the slot, the gateway sees memclaw but never calls `register()`. The automated installer handles this automatically.

> Alternatively, use the Plugin Manager's **Fix Configuration** button or the OpenClaw CLI:
> ```bash
> openclaw plugins disable memory-core
> openclaw plugins enable memclaw
> ```

**Optional — enable ContextEngine (Tier 2):** For full auto read/write loop, also set the contextEngine slot:

```json
{
  "plugins": {
    "slots": {
      "memory": "memclaw",
      "contextEngine": "memclaw"
    }
  }
}
```

Without the `contextEngine` slot, you still get all 9 agent-facing tools, prompt education, flush plan, and memory runtime — but no automatic read/write loop.

**Verify** — restart OpenClaw and check startup logs:

```
[memclaw] Auto-educated 20 workspace(s), SKILL.md in 20, TOOLS.md in 20, AGENTS.md in 20
[memclaw] ContextEngine 'memclaw' registered
[memclaw] Smoke test passed (score: 0.953)
```

The node will appear in the Fleet page (`/ui/fleet.html`) within 60 seconds.

### Plugin internals

The plugin registers 12 tools and runs several lifecycle systems:

- **ContextEngine** — 7 lifecycle hooks: `bootstrap` (smoke test), `ingest` (message buffering + persistence), `assemble` (token-budget-aware recall injection), `compact` (persist summaries), `afterTurn` (auto-write turn summaries), `prepareSubagentSpawn`, `onSubagentEnded`
- **Memory runtime** — API-backed `search()` and `get()` replacing file-based `memory-core`
- **Heartbeat** — every 60 seconds, POSTs node status (agents, tools, OS, IP, plugin version, setup_status) to `/api/fleet/heartbeat`. MemClaw responds with any pending commands
- **Commands** — the plugin processes HMAC-verified commands from the heartbeat response:
  - `deploy` — fetch all source files to memory, backup originals, write + build, rollback on failure
  - `educate` — write prompts to agent HEARTBEAT.md files + write SKILL.md, TOOLS.md, AGENTS.md to workspaces
  - `ping` — health check round-trip
  - `restart` — gateway restart
- **Auto-education** — on first load, writes SKILL.md, TOOLS.md, AGENTS.md to all agent workspaces. New workspaces are auto-educated on heartbeat
- **Auto-resolve** — `tenant_id` is resolved from the API key at startup, so agents never need to specify it
- **Gateway RPC** — exposes `memclaw.status`, `memclaw.deploy`, `memclaw.deploy.status`, `memclaw.educate`, `memclaw.allowlist.check`, `memclaw.allowlist.fix` methods

### Educating agents

On first plugin load, agents are **auto-educated** — the plugin writes SKILL.md, TOOLS.md, and AGENTS.md to all agent workspaces automatically. The `.educated` flag at `~/.openclaw/plugins/memclaw/.educated` prevents re-running on subsequent restarts.

For manual or targeted education via the Fleet page:

1. Click a node → expand an agent → **Educate** (targets one agent) or use the **Educate Agents** button (targets all or selected)
2. Review/edit the education prompt (pre-filled with default instructions)
3. Click **Queue Educate Command** — delivered on the next heartbeat (≤60 seconds)
4. The plugin writes the prompt to each agent's `HEARTBEAT.md` and updates SKILL.md, TOOLS.md, AGENTS.md in their workspace
5. Each write is verified by read-back — the command result reports `verified` count and any per-workspace failures
6. Agents process the prompt on their next heartbeat and update their own TOOLS.md, AGENTS.md, SOUL.md, IDENTITY.md

The **Agent Education Status** section in Plugin Manager shows green checkmarks for agents that have been educated.

---

## 4. Agent Trust Levels

MemClaw enforces a 4-tier trust system for agents. Agents are auto-registered on their first `memclaw_write` call at trust level 1.

| Level | Name | Permissions |
|---|---|---|
| 0 | `restricted` | No read or write access. Use to temporarily disable an agent |
| 1 | `standard` | Read and write within own fleet only (default for new agents) |
| 2 | `cross_fleet` | Read across all fleets in the tenant; write within own fleet only |
| 3 | `admin` | Read and write across all fleets; can delete memories |

### How it works

- On first write, the agent is auto-registered with trust level 1 and the `fleet_id` from that write becomes its "home fleet"
- Trust level is enforced on every API call — an agent at level 1 attempting a cross-fleet search gets a 403
- The admin API key bypasses all trust enforcement

### Managing trust levels

**Via the Manage page** (`/ui/tenant-admin.html`): The Agents tab shows all registered agents with their trust levels, home fleets, and last-seen timestamps. Click to adjust trust.

**Via API:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/agents?tenant_id=` | GET | List all registered agents with trust levels |
| `/api/agents/{agent_id}?tenant_id=` | GET | Single agent detail (trust level, home fleet, stats) |
| `/api/agents/{agent_id}/trust?tenant_id=` | PATCH | Update trust level (body: `{"trust_level": 2}`) |

### The Manage page

The Manage page (`/ui/tenant-admin.html`) is the tabbed tenant admin dashboard, accessible after sign-in. Usage stats are always visible at the top, with four tabs:

- **Agents** — view all registered agents, their trust levels, home fleets, and activity; adjust trust levels
- **API Keys** — create and revoke tenant-scoped API keys
- **Configuration** — per-tenant settings in three cards: **Models** (unified LLM provider/model for enrichment, recall, entity extraction + configurable fallback LLM for automatic failover + embedding provider/model), **Features** (enrichment, entity extraction, recall synthesis, graph retrieval, recall boost, semantic dedup, auto-crystallize, lifecycle automation, auto-chunking, agent approval), and **API Keys** (encrypted at rest). Agents can also self-tune their own search retrieval parameters (top_k, min_similarity, fts_weight, freshness, recall boost, graph hops, etc.) via the `memclaw_tune` tool
- **Crystallizer** — memory health + crystallization results: overall health score, hygiene issues, coverage metrics, type/status distributions, recall stats, crystallization actions taken, and report history. Run on-demand or nightly
- **Activity** — full audit trail of writes, deletes, and admin actions

---

## 5. Agent Prompts and Examples

### Agent system prompt

Add this to your agent's system prompt (or use Agent Education to let agents self-configure):

```
You have access to MemClaw, a shared memory system used by all agents.

BEFORE starting any task:
- Use memclaw_recall for semantic + keyword search with graph expansion
- Set include_brief=true when you want a concise LLM-summarized paragraph
  instead of raw results
- Include fleet_id to scope to this fleet, omit for tenant-wide search
- Filter by status="active" to skip deleted/archived memories
- Use valid_at for point-in-time queries (OpenClaw plugin and REST API only)

AFTER completing work:
- Store findings with memclaw_write — just provide content
- For batch writes, pass items=[...] (up to 100) to the same tool — batches
  embeddings and enrichment for much lower latency than looped single writes
- Type, weight, status, title, summary, tags are auto-inferred by LLM
- Dates auto-extracted: "deadline March 30" → ts_valid_end
- Contradictions auto-detected: conflicting older memories marked outdated
- Long content (>2000 chars) is auto-chunked into atomic facts
- Set visibility: "scope_agent" (you only), "scope_team" (default), "scope_org" (all fleets)
- Optionally override memory_type, weight, status
- RDF triples (subject_entity_id, predicate, object_value) available via OpenClaw plugin and REST API

MANAGING EXISTING MEMORIES:
- Use memclaw_manage with op="update" to correct content or metadata
- Use memclaw_manage with op="transition" to change status
- Use memclaw_manage with op="delete" to soft-delete
- Use memclaw_manage with op="read" to inspect a single memory by id
- Only provide fields you want to change — others are preserved
- If content changes, embedding and entities are re-extracted automatically
- You can only modify your own memories unless you have admin trust level

STATUS LIFECYCLE:
- Use memclaw_manage op="transition" when things change
- confirmed (done), cancelled (abandoned), outdated (superseded)
- Search status="pending" for unresolved items

VISIBILITY & CROSS-FLEET:
- Default visibility is "scope_team" — shared within your fleet
- Set visibility: "scope_org" to share across all fleets in the organization
- Set visibility: "scope_agent" for agent-only notes
- Use fleet_ids in recall to query multiple fleets at once

ENTITIES & GRAPH:
- Auto-extracted from every write — no manual creation needed
- Fuzzy entity matching: "OpenAI" and "Open AI" are auto-merged (cosine similarity ≥ 0.85)
- Recall automatically expands through entity relations (up to 2 hops)
  Example: searching "Project Atlas" also finds memories about people who work on Atlas
- Use memclaw_entity_get for direct relationship and linked memory inspection

OUTCOME REPORTING (Karpathy Loop):
- After acting on recalled memories, report what happened with memclaw_evolve
- outcome_type: "success" / "failure" / "partial"; pass related_ids=[...]
- Successful recalls get reinforced; failures generate preventive rules
- Use memclaw_insights periodically to surface contradictions, stale
  knowledge, cross-agent divergence, and emerging patterns
```

### Example: Multi-agent workflow

> **Note:** `tenant_id` is resolved from your API key (MCP) or auto-filled from the plugin env — agents never need to pass it. `agent_id` is also auto-filled (defaults to `"mcp-agent"` for MCP or the plugin env value for OpenClaw).

**Scenario:** Researcher discovers info, Planner uses it, Support benefits later.

**Step 1 — Researcher stores a finding (visible to all fleets):**

```json
{
  "tool": "memclaw_write",
  "parameters": {
    "content": "Customer X uses PostgreSQL 16 in production on GKE. They process 2M transactions/day.",
    "source_uri": "crm://customer-x/infrastructure",
    "visibility": "scope_org"
  }
}
```

LLM enrichment auto-classifies as `fact`, weight `0.9`, title "Customer X: PostgreSQL 16 on GKE", tags `["customer-x", "postgresql", "gke"]`. Entity extraction identifies "Customer X" (org), "PostgreSQL 16" (technology), "GKE" (technology). Visibility `scope_org` means all fleets can see this.

**Step 2 — Planner recalls:**

```json
{
  "tool": "memclaw_recall",
  "parameters": {
    "query": "which customers use PostgreSQL and what is their scale?",
    "memory_type": "fact"
  }
}
```

Returns the researcher's finding. Graph-enhanced retrieval expands through entity relations — memories linked to matching entities get a 1.3x boost, 1-hop neighbors 1.2x, and 2-hop neighbors 1.1x.

**Step 3 — Planner stores a decision:**

```json
{
  "tool": "memclaw_write",
  "parameters": {
    "content": "Customer X should be migrated to managed Postgres in Phase 2 due to high transaction volume."
  }
}
```

**Step 4 — Support agent picks it up:**

```json
{
  "tool": "memclaw_recall",
  "parameters": {
    "query": "Customer X database setup and migration plans"
  }
}
```

Returns both the fact and the decision — full context without agents needing to talk to each other.

### Example: Batch write (after processing a document)

**Scenario:** Agent has extracted several findings and stores them all at once via the batch form of `memclaw_write`.

```json
{
  "tool": "memclaw_write",
  "parameters": {
    "items": [
      {"content": "Customer A uses PostgreSQL 16 in production on GKE"},
      {"content": "Customer A processes 2M transactions per day"},
      {"content": "Customer A's DBA team prefers managed database services"},
      {"content": "Customer A contract renewal is scheduled for Q3 2026"}
    ]
  }
}
```

Returns per-item results with `created`/`duplicate`/`error` status for each item, plus overall counts. Much faster than 4 individual single-`content` calls — embeddings are batched into a single API call and enrichment runs in parallel. Pass the batch form (`items`) exactly when you have more than one memory; `items` is mutually exclusive with `content`.

### Example: Entity lookup

```json
{
  "tool": "memclaw_entity_get",
  "parameters": {
    "entity_id": "c5d5ee20-78a4-4dd0-b9ca-6a41809a6ca5"
  }
}
```

Returns entity attributes, all linked memories, and outgoing relations (e.g., Customer A -> uses -> PostgreSQL 16).

### Example: Contradiction resolution (OpenClaw plugin / REST API)

> RDF triple fields (`subject_entity_id`, `predicate`, `object_value`) are available via the OpenClaw plugin and REST API. MCP clients trigger contradiction detection through semantic similarity only (no explicit RDF triples).

**Original memory exists:**
```json
{
  "id": "aaa-111",
  "content": "Sarah Chen lives in Tel Aviv, Israel",
  "subject_entity_id": "e663...",
  "predicate": "lives_in",
  "object_value": "Tel Aviv, Israel",
  "status": "active"
}
```

**Agent writes contradicting memory:**
```json
{
  "tool": "memclaw_write",
  "parameters": {
    "content": "Sarah Chen moved to Berlin, Germany",
    "subject_entity_id": "e663...",
    "predicate": "lives_in",
    "object_value": "Berlin, Germany"
  }
}
```

**Response includes:**
```json
{
  "id": "bbb-222",
  "status": "active",
  "superseded_by": [
    {
      "old_memory_id": "aaa-111",
      "old_status": "outdated",
      "reason": "rdf_conflict",
      "old_content_preview": "Sarah Chen lives in Tel Aviv, Israel"
    }
  ]
}
```

The old memory is automatically marked `outdated` with `supersedes_id` pointing to the new one.

---

## 6. Reference

### Memory types

Auto-classified by LLM on every write. Agents can override with `memory_type`.

| Type | Use for | Default status | Example |
|---|---|---|---|
| `fact` | Durable knowledge | `active` | "Customer A uses Postgres 16" |
| `episode` | Events that happened | `active` | "Deployed v2.3 on March 10" |
| `decision` | Choices made | `active` | "Chose managed Postgres over self-hosted" |
| `preference` | User/org preferences | `active` | "Customer B prefers email over Slack" |
| `task` | Work items | `pending` | "Migrate Customer X by Q3" |
| `semantic` | Conceptual knowledge | `active` | "pgvector supports HNSW and IVFFlat" |
| `intention` | Goals not yet acted on | `active` | "Planning to evaluate FalkorDB" |
| `plan` | Step sequences | `pending` | "Phase 1: schema, Phase 2: data, Phase 3: cutover" |
| `commitment` | Promises to others | `pending` | "Promised timeline by Friday" |
| `action` | Steps in progress | `active` | "Started the migration script" |
| `outcome` | Results of work | `confirmed` | "Migration: 2M rows in 47 min, zero errors" |
| `cancellation` | Cancelled items | `active` | "Cancelled FalkorDB eval" |
| `rule` | Preventive guardrails | `active` | "Never deploy schema changes on Fridays" (often auto-generated by `memclaw_evolve` after a failure) |
| `insight` | Analytical findings | `active` | "5 memories contradict each other about customer X's region" (auto-generated by `memclaw_insights`) |

### Memory status lifecycle

| Status | Meaning | How it gets set |
|---|---|---|
| `active` | Current and valid (default) | LLM default for most types |
| `pending` | Not yet confirmed | LLM default for tasks, plans, commitments |
| `confirmed` | Verified or completed | Agent via `memclaw_manage` op=`transition` or LLM for outcomes |
| `cancelled` | Explicitly cancelled | Agent via `memclaw_manage` op=`transition` |
| `outdated` | Superseded by newer info | Auto-set by contradiction detection (RDF conflict) or lifecycle automation (past `ts_valid_end`) |
| `conflicted` | Contradicts another memory | Auto-set by contradiction detection (semantic); needs review |
| `archived` | Preserved but no longer current | Agent via `memclaw_manage` op=`transition` or lifecycle automation (stale, low-weight, never-recalled) |
| `deleted` | Soft-deleted | Set on `memclaw_manage` op=`delete` or DELETE API call |

### RDF triples

Attach structured subject-predicate-object triples to memories for graph-friendly retrieval:

```json
{
  "subject_entity_id": "c5d5ee20-...",
  "predicate": "uses",
  "object_value": "PostgreSQL 16"
}
```

Enables contradiction detection (same subject+predicate, different object -> old memory marked `outdated`).

### Memory visibility

Controls who can see a memory. Set on write via `visibility` field.

| Level | Who can see | Use for |
|---|---|---|
| `scope_agent` | Only the creating agent | Personal notes, drafts, scratch work |
| `scope_team` | All agents in the same fleet (default) | Team-scoped knowledge |
| `scope_org` | All agents across all fleets | Company-wide facts, cross-team decisions |

Search respects visibility automatically. Agents see: all `scope_org` memories + `scope_team` memories in their fleet + their own `scope_agent` memories. Admin API key sees all except `scope_agent`.

### Auto-chunking

Content exceeding 2,000 characters is automatically split into atomic facts via LLM:

- Creates a **parent memory** with the full content (tagged `auto_chunked: true`)
- Creates **child memories** for each extracted fact (tagged `source: "auto_chunk"`, linked to parent)
- Both parent and children inherit type, weight, status, visibility
- Togglable per tenant via `auto_chunk_enabled` setting
- Falls back to single-memory write if chunking fails

> **Note:** This is a behavior change — integrations sending long content will now get multiple memories instead of one. Disable per tenant if needed.

### Lifecycle automation

Background scheduler runs every 24 hours and automatically:

1. **Expires** — active memories past `ts_valid_end` → status `outdated`
2. **Archives** — memories older than 180 days with weight ≤ 0.3 and zero recalls → status `archived`
3. **Crystallizes** — triggers crystallization when active memory count exceeds 1,000

Togglable per tenant via `lifecycle_automation_enabled` setting.

### Temporal validity

- `ts_valid_start` / `ts_valid_end` — auto-extracted from content by LLM, or set explicitly
- Search with `valid_at` to return only memories valid at a point in time
- Memories without temporal bounds are always considered valid

### Batch Write

Available as the batch form of the `memclaw_write` tool (MCP + OpenClaw plugin, pass `items=[...]`) and the `POST /api/memories/bulk` REST endpoint. Writes up to 100 memories in a single request. Optimized for throughput:

- **Batch embeddings** — single API call for all texts instead of N calls
- **Parallel enrichment** — LLM enrichment runs concurrently (bounded at 10)
- **Batch dedup** — one `WHERE content_hash IN (...)` query + intra-batch duplicate detection
- **Single transaction** — all memories inserted and committed at once
- **Single rate-limit check** — quota verified once for the whole batch

```json
{
  "tenant_id": "acme",
  "fleet_id": "engineering",
  "agent_id": "researcher-1",
  "items": [
    {"content": "Customer A uses PostgreSQL 16 in production"},
    {"content": "Customer B migrated to managed Postgres last quarter", "memory_type": "fact"},
    {"content": "Customer C prefers email notifications", "weight": 0.8}
  ]
}
```

Response:

```json
{
  "created": 3,
  "duplicates": 0,
  "errors": 0,
  "results": [
    {"index": 0, "status": "created", "id": "..."},
    {"index": 1, "status": "created", "id": "..."},
    {"index": 2, "status": "created", "id": "..."}
  ],
  "bulk_ms": 450
}
```

Each item in `items` supports the same fields as a single-`content` write (memory_type, weight, status, source_uri, entity_links, RDF triples, temporal bounds). `tenant_id`, `fleet_id`, and `agent_id` are set once at the top level. When calling `memclaw_write`, pass exactly one of `content` (single) or `items` (batch).

Duplicates (exact content hash match against DB or within the batch) are reported as `"status": "duplicate"` with `duplicate_of` pointing to the existing memory ID. All enrichment, entity extraction, and contradiction detection run the same as single writes.

### Deduplication

Content-hash rejects exact duplicates within a tenant+fleet scope (HTTP 409). Same content can exist in different fleets.

### Troubleshooting

| Issue | Fix |
|---|---|
| Plugin tools don't appear | Ensure all three `plugins` keys are set in `openclaw.json`: `allow`, `entries`, and `load.paths`. Restart OpenClaw |
| Tools not in agent sessions | Auto-fixed on first plugin load (adds v1.0 names, removes stale pre-v1.0 names). If it persists after restart, run `openclaw gateway memclaw.allowlist.fix` or check that `MEMCLAW_AUTO_FIX_CONFIG` is not set to `false` |
| Plugin allowed but not loading | Missing `plugins.entries.memclaw.enabled: true` or `plugins.load.paths` entry — the installer and Fix Configuration set both |
| All config issues | Use the "Fix Configuration" button in Fleet Browser Plugin Manager to auto-fix all settings |
| `ECONNREFUSED` | Check `MEMCLAW_API_URL`, ensure API is running |
| 401 Unauthorized | Check `MEMCLAW_API_KEY` env var on gateway |
| 403 Forbidden | Key used for wrong tenant, or agent trust level too low |
| 409 Conflict | Duplicate content — safe to ignore |
| Empty search results | Verify tenant_id, check fleet_id scope, write test memories |
