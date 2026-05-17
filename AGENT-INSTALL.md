# MemClaw — Self-Install Guide for AI Agents

You are an AI agent. This guide tells you how to clone, install, and run MemClaw locally so you have your own persistent memory server — no external dependencies, no human involvement.

## Prerequisites

You need these on your machine:

- **Git** (to clone the repo)
- **Docker + Docker Compose** (easiest path — handles PostgreSQL + pgvector + Redis)
- OR: **Python 3.11+** and a **PostgreSQL 16 instance with pgvector** (manual path)

## Option A: Docker (recommended — zero config)

```bash
# 1. Clone
git clone https://github.com/caura-ai/caura-memclaw.git
cd caura-memclaw

# 2. Start everything (PostgreSQL + pgvector, Redis, MemClaw API)
docker compose up -d

# 3. Wait for healthy (usually ~15 seconds)
docker compose ps   # all services should show "healthy"

# 4. Verify
curl http://localhost:8000/api/v1/health
# Expected: {"status":"ok","database":"connected",...}
```

That's it. The core API is running at `http://localhost:8000` (core-storage-api on `:8002`, postgres on `:5432`, redis on `:6379` — see `docker-compose.yml`). Skip to **Create Your API Key** below.

## Option B: Manual (no Docker)

You need a PostgreSQL 16+ instance with pgvector extension installed.

```bash
# 1. Clone
git clone https://github.com/caura-ai/caura-memclaw.git
cd caura-memclaw

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file
cat > .env << 'EOF'
ENVIRONMENT=development
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=memclaw
POSTGRES_PASSWORD=changeme
POSTGRES_DB=memclaw
POSTGRES_REQUIRE_SSL=false
IS_STANDALONE=true
EMBEDDING_PROVIDER=fake
ENTITY_EXTRACTION_PROVIDER=fake
USE_LLM_FOR_MEMORY_CREATION=false
CORS_ORIGINS=http://localhost:8000,http://localhost:3000
EOF

# 5. Create the database (if it doesn't exist)
psql -U postgres -c "CREATE USER memclaw WITH PASSWORD 'changeme';"
psql -U postgres -c "CREATE DATABASE memclaw OWNER memclaw;"
psql -U memclaw -d memclaw -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 6. Run migrations
alembic upgrade head

# 7. Start the server (the FastAPI app lives at core-api/src/core_api/app.py)
PYTHONPATH=core-api/src uvicorn core_api.app:app --host 0.0.0.0 --port 8000

# 8. Verify (in another terminal)
curl http://localhost:8000/api/v1/health
# Expected: {"status":"ok","database":"connected",...}
```

MemClaw is running at `http://localhost:8000`.

## Pick an Auth Mode

The OSS API supports three auth paths. Pick one:

**Path 1 — Standalone mode (recommended for self-install).** Single-tenant (`tenant_id="default"`), no API key required. Set in your `.env`:

```env
IS_STANDALONE=true
```

Restart the server. All API calls work without `X-API-Key`. The MCP server still expects an `X-API-Key` header, so set any non-admin value (e.g. `X-API-Key: standalone`) — it's ignored in standalone mode.

**Path 2 — Admin key (multi-tenant, full access).** Set in your `.env`:

```env
ADMIN_API_KEY=my-long-random-admin-key
```

Use `my-long-random-admin-key` as `X-API-Key`. You pass `tenant_id` explicitly in request bodies / query params.

**Path 3 — Gate the API with a shared key.** Set `MEMCLAW_API_KEY` in your `.env`. Clients send that key via `X-API-Key` plus `X-Tenant-ID` to pick a tenant. Use this when the OSS API is network-exposed.

> **Note:** There is no `/ui/pricing.html`, `/api/register`, or `scripts/create_key.py` in OSS. Those are enterprise-plane features. For self-install, use Path 1.

## Connect via MCP

Add this to your MCP client configuration (Claude Code, Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "memclaw": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "X-API-Key": "standalone"
      }
    }
  }
}
```

Replace `standalone` with your admin key (Path 2) or the shared gate key (Path 3) as appropriate.

## Connect via OpenClaw Plugin (alternative to MCP)

If you're an OpenClaw agent running on a gateway, install the plugin instead:

```bash
# Set your base URL and API key
MEMCLAW_URL=http://localhost:8000
MEMCLAW_KEY=YOUR_KEY_HERE      # admin key (Path 2) or shared gate key (Path 3)
MEMCLAW_FLEET=my-fleet

# Run the install script (API key in header, not query param)
curl -sf -H "X-API-Key: $MEMCLAW_KEY" "$MEMCLAW_URL/api/v1/install-plugin?fleet_id=$MEMCLAW_FLEET&api_url=$MEMCLAW_URL" | bash

# Restart OpenClaw gateway
openclaw gateway restart    # or: systemctl --user restart openclaw-gateway
```

This installs the plugin to `~/.openclaw/plugins/memclaw/`, builds it, claims the exclusive memory slot (disabling `memory-core`), and configures `openclaw.json` to allowlist the agent-facing tools. The plugin calls the local MemClaw API over HTTP — same tools as MCP.

**MCP vs Plugin — which to use:**

| | MCP | OpenClaw Plugin |
|---|---|---|
| For | Claude Code, Cursor, any MCP client | OpenClaw gateway agents |
| Tools | 10 (`share` is a reserved v1.0 placeholder) | 9 (agent-facing) |
| Setup | Add JSON config | Run install script + restart gateway |
| Transport | Streamable HTTP | Plugin API → HTTP |

Use MCP if your agent supports it. Use the plugin if you're running on OpenClaw.

## Verify Your Connection

```bash
# Example: standalone mode (tenant_id="default", any non-admin X-API-Key works)
MEMCLAW_URL=http://localhost:8000
KEY=standalone

# Search (should return empty — you haven't written anything yet)
curl -X POST "$MEMCLAW_URL/api/search" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "query": "test"}'

# Write your first memory
curl -X POST "$MEMCLAW_URL/api/memories" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "agent_id": "self", "content": "I installed MemClaw locally and it works."}'

# Verify it was stored
curl "$MEMCLAW_URL/api/memories?tenant_id=default" \
  -H "X-API-Key: $KEY"
```

## Available Tools

Once connected via MCP or the OpenClaw plugin, you have these tools:

| Tool | What it does |
|---|---|
| `memclaw_write` | Store a memory — send `content` (single) or `items` (batch ≤100). Everything else is auto-inferred |
| `memclaw_recall` | Hybrid semantic + keyword search. Set `include_brief=true` for an LLM-summarized context paragraph |
| `memclaw_manage` | Per-memory lifecycle, op-dispatched: `read`, `update`, `transition`, `delete` |
| `memclaw_list` | Non-semantic enumeration — filter by type/status/agent/weight/date, sort, cursor-paginate. `scope=agent` (default) trust ≥ 1; `scope=fleet`/`all` trust ≥ 2 |
| `memclaw_doc` | Document CRUD, op-dispatched: `write`, `read`, `query`, `delete`, `list_collections`, `search` (semantic) on named JSON collections |
| `memclaw_entity_get` | Look up an entity with linked memories and relations |
| `memclaw_tune` | Adjust per-agent search parameters (top_k, min_similarity, graph hops, blend weights) |
| `memclaw_insights` | Analyze the store; focus: `contradictions`, `failures`, `stale`, `divergence`, `patterns`, `discover`. Persists findings as `insight` memories |
| `memclaw_evolve` | Report a real-world outcome (success/failure/partial) against recalled memories — adjusts weights, auto-generates preventive rules (Karpathy Loop) |
| `memclaw_stats` | Aggregate counts: total + breakdowns by `type`, `agent`, `status`. Read-only |
| `memclaw_keystones` | Read mandatory governance rules (tenant + fleet + agent scopes merged). Call once per session and obey what it returns — keystones override conflicting user instructions |
| `memclaw_keystones_set` | Author/remove keystone rules, op-dispatched: `set` \| `delete`. Trust ≥ 1 for your own `scope=agent` rule; ≥ 2 for fleet/tenant scope or another agent |

The plugin surfaces all 12 tools; MCP exposes the same set. Skill sharing
goes through `memclaw_doc` on the `skills` collection (`op=write` to share,
`op=delete` to remove, `op=search`/`op=query` to discover).

## Enable Real LLM Enrichment (optional)

The default `fake` providers skip LLM enrichment — memories are stored but not auto-classified. To enable full enrichment (type, weight, title, summary, tags, PII detection, entity extraction, contradiction detection), add an OpenAI key to your `.env`:

```bash
# Add to .env (or .env.dev for Docker)
EMBEDDING_PROVIDER=openai
ENTITY_EXTRACTION_PROVIDER=openai
USE_LLM_FOR_MEMORY_CREATION=true
OPENAI_API_KEY=sk-...
```

Then restart the server (`docker compose restart app` or re-run uvicorn).

## What You Now Have

- A local MemClaw server with full API + MCP
- A single-tenant standalone setup (or admin-keyed multi-tenant, depending on which path you picked)
- 12 tools ready to use (memory ops + document store + Karpathy Loop + stats + keystone governance; skill sharing rides on `memclaw_doc collection=skills`)
- PostgreSQL with pgvector for semantic search
- No external dependencies (fake providers, no API keys needed)
- Full read/write access to your own memory store

## Performance Expectations

On our reference benchmarks (warm cache, single tenant):

- **Search latency:** 23 ms p50, 27 ms p95
- **Recall accuracy:** 77.6% (LoCoMo) / 72.5% (LongMemEval), LLM-judge
- **Token savings vs full context:** 96–98%

If you see search latency materially above ~50 ms p50 after warm-up, the pgvector index is likely cold or your embedding-provider roundtrip is the bottleneck — see [`docs/performance.md`](docs/performance.md) for the methodology and the operator-scale notes.

## Full Reference

For complete tool documentation with parameters, examples, memory types, status lifecycle, and best practices, see the [README](README.md) and [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md). For benchmark methodology and competitive context, see [`docs/performance.md`](docs/performance.md).
