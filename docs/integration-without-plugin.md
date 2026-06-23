# Integrate with memclaw without the plugin

**Audience:** developers building Python, Node, or any other SDK client against `memclaw.net` or a self-hosted memclaw instance, without installing the OpenClaw plugin runtime.

**Time to first tool call:** ~5 minutes.

> **One credential model.** Every API credential — tenant-scoped, agent-scoped, install, cross-tenant — uses the `mc_` prefix on the wire and lives in one underlying credential table. Scope is bound to the credential row at mint time, not encoded in the prefix. Pre-existing `mca_…` / `mci_…` keys continue to authenticate via back-compat; new mints all return `mc_…`.

---

## What you need

- A tenant-scoped `mc_` credential — get one from `memclaw.net/settings/api-keys` (or self-host).
- An MCP client. Examples below use `mcp` (Python) and `curl`. The same flow works with `anthropic` (Python SDK's remote-MCP integration), `openai`, or any client that speaks MCP streamable-http.

---

## 1. Mint an agent-scoped credential

Every long-lived integration should bind to a named agent identity rather than calling under a tenant-scoped credential. Two ways:

- **Dashboard (recommended for humans):** `memclaw.net/settings/organization/api-credentials` — single-card wizard, one-time raw-key reveal, manages cross-tenant + read-scope settings.
- **API (for scripted provisioning, shown below):** `POST /api/v1/admin/agent-keys/provision` — atomic call that creates the Agent row eagerly so subsequent trust-elevation or fleet-assignment endpoints work immediately:

```bash
curl -X POST https://memclaw.net/api/v1/admin/agent-keys/provision \
  -H "X-API-Key: $MC_TENANT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "quote-agent-na",
    "label": "north-america CRM",
    "initial_trust": 1,
    "initial_fleet": "na-sales"
  }'
```

Response:

```json
{
  "id": "…",
  "tenant_id": "…",
  "agent_id": "quote-agent-na",
  "raw_key": "mc_…",
  "agent_row_created": true,
  "created_at": "…"
}
```

Save `raw_key` immediately — it's only returned once. The returned credential uses the `mc_` prefix regardless of kind; the gateway derives scope (`tenant` / `agent` / `cross-tenant`) from the credential row, not the prefix. `agent_row_created: true` confirms the Agent row exists and `PATCH /agents/quote-agent-na/trust` will work without a synthetic first write.

**Optional fields** on the provision request:

- `initial_trust` — `0`, `1`, `2`, `3` (default `1`).
- `initial_fleet` — fleet membership; absent = no fleet (writes default to tenant-wide scope).
- `display_name` — human-readable name surfaced on the dashboard.

---

## 2. Verify your identity (`/whoami`)

Before making real tool calls, confirm memclaw resolves your credentials the way you expect:

```bash
curl https://memclaw.net/api/v1/whoami \
  -H "X-API-Key: $AGENT_KEY"
```

```json
{
  "tenant_id": "your-tenant-id",
  "agent_id": "quote-agent-na",
  "auth_source": "gateway-header",
  "via_gateway": true
}
```

If `agent_id` is `null` or doesn't match what you provisioned, your credential isn't recognized as agent-scoped. Common causes:
- You're sending a tenant-scoped credential, not the agent-scoped one you provisioned.
- The credential was revoked or rotated.
- A proxy in front of memclaw is stripping the `X-API-Key` header.

> **Latency expectation:** `POST /search` returns 23 ms p50 / 27 ms p95 warm on our reference benchmarks. Recall (`memclaw_recall` / `POST /recall`) sits in the same band — it wraps search plus a small scoring step. See [`performance.md`](performance.md) for the full numbers and methodology.

---

## 3. Open an MCP session

memclaw speaks MCP streamable-http at `/mcp` (the trailing slash is optional; both `/mcp` and `/mcp/` work).

### Authentication: two headers, your choice

memclaw accepts the credential on either of these — pick whichever your SDK supports:

| Header | When to use |
|---|---|
| `X-API-Key: mc_…` | Canonical. Use if you control the request shape. |
| `Authorization: Bearer mc_…` | OAuth-style. Required by Anthropic's remote-MCP integration and other SDKs that only emit `Authorization` headers. |

Legacy `mca_…` / `mci_…` keys continue to authenticate on both headers via back-compat. JWTs from the dashboard are also accepted via `Authorization: Bearer <jwt>`; memclaw distinguishes them by trying JWT decode first.

### Python (the `mcp` library)

```python
import asyncio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    headers = {"X-API-Key": "mc_..."}   # agent-scoped credential
    url = "https://memclaw.net/mcp/"

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            result = await session.call_tool(
                "memclaw_write",
                {"content": "First memory from the Python harness."},
            )
            print(result)

asyncio.run(main())
```

### Anthropic SDK (remote-MCP)

```python
from anthropic import Anthropic

client = Anthropic()
msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Save this note: pricing meets 10 May."}],
    extra_body={
        "mcp_servers": [
            {
                "type": "url",
                "url": "https://memclaw.net/mcp/",
                "name": "memclaw",
                "authorization_token": "mc_...",   # agent-scoped credential
            }
        ]
    },
)
print(msg.content)
```

The SDK forwards `authorization_token` as `Authorization: Bearer mc_…`. memclaw recognises that shape and resolves your tenant + agent identity from the credential row.

---

## 4. Elevate trust (when needed)

The default `trust_level=1` lets the agent write to its home fleet. Elevate when you need cross-fleet writes, keystone authoring, or deletes:

```bash
curl -X PATCH "https://memclaw.net/api/v1/agents/quote-agent-na/trust?tenant_id=$TENANT_ID" \
  -H "X-API-Key: $MC_TENANT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"trust_level": 2}'
```

Trust levels:
- `0` — read-only.
- `1` — write to home fleet.
- `2` — cross-fleet read.
- `3` — cross-fleet write + delete + update others' memories.

If you provisioned with `initial_trust`, this step is already done — confirm with `/whoami`.

---

## 5. Author a keystone (governance rule)

Keystones are mandatory rules that override conflicting instructions. Authoring a
`scope=fleet`/`scope=tenant` rule needs trust ≥ 2 (see above).

```bash
curl -X POST "https://memclaw.net/api/v1/memclaw/keystones" \
  -H "X-API-Key: $MC_TENANT_KEY" \
  -H "X-Agent-ID: quote-agent-na" \
  -H "Content-Type: application/json" \
  -d '{
        "tenant_id": "'"$TENANT_ID"'",
        "doc_id": "no-secrets-in-logs",
        "title": "No secrets in logs",
        "content": "Never log credentials or API keys.",
        "scope": "tenant",
        "weight": "high"
      }'
```

`doc_id` is **required** — a stable kebab-case slug you choose
(`^[a-z0-9][a-z0-9._-]{0,99}$`). It's the rule's identity: re-POSTing the same
`doc_id` upserts (edits) that rule rather than creating a duplicate. `weight` is
`low` / `med` / `high` (stored as `25` / `50` / `100`). For `scope=fleet`/`agent`
also pass `fleet_id`; `scope=agent` additionally takes the target `agent_id`
(omit `agent_id` for `tenant`/`fleet`).

---

## End-to-end bootstrap, one block

```bash
TENANT_KEY=mc_...   # tenant-scoped credential
AGENT_ID="quote-agent-na"
FLEET_ID="na-sales"

# 1. Provision agent + Agent row + trust + fleet in one call.
RESP=$(curl -s -X POST https://memclaw.net/api/v1/admin/agent-keys/provision \
  -H "X-API-Key: $TENANT_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"$AGENT_ID\",\"initial_trust\":1,\"initial_fleet\":\"$FLEET_ID\"}")
AGENT_KEY=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['raw_key'])")
# AGENT_KEY is an mc_… agent-scoped credential.

# 2. Verify.
curl -s https://memclaw.net/api/v1/whoami -H "X-API-Key: $AGENT_KEY"

# 3. Use.
curl -s https://memclaw.net/api/v1/memories \
  -H "X-API-Key: $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"tenant_id\":\"$TENANT_ID\",\"agent_id\":\"$AGENT_ID\",\"fleet_id\":\"$FLEET_ID\",\"content\":\"Hello world\"}"
```

Four steps, one round-trip per agent. The bootstrap dance from earlier integration attempts (provision → fake-write → patch trust → seed) is no longer necessary.

---

## Idempotency

A write of identical content (same `agent_id`, same `fleet_id`) is retry-safe via MCP:

- First call → `201` with the new memory id.
- Identical retry → `200` with `{ "status": "duplicate", "existing_id": "…" }`.

Cross-agent writes of identical content no longer collide — each agent gets its own record.

---

## Common pitfalls

- **`POST /provision` returns the raw key once.** Save it before the response goes out of scope.
- **`PATCH /agents/{id}/trust` returns 404 immediately after provisioning.** This should not happen post-2026-05-13; if it does, the Agent row was not materialized atomically. Check `whoami` and `GET /api/v1/agents/{id}`.
- **`/mcp` returns 401 with an `Authorization: Bearer mc_…` (or legacy `mca_…`) header but works with `X-API-Key`.** Make sure you're hitting a memclaw build dated 2026-05-13 or later — earlier builds rejected non-JWT bearer tokens.
- **Streaming client hangs on initialize.** If hitting `/mcp` (no slash) caused a hang on older builds, append the trailing slash or upgrade — current builds serve both paths without redirect.

---

## Reference

- `POST /api/v1/admin/agent-keys/provision` — atomic provisioning (this guide).
- `GET /api/v1/whoami` — identity probe.
- `GET /api/v1/agents/{id}?tenant_id=...` — agent detail.
- `PATCH /api/v1/agents/{id}/trust?tenant_id=...` — change trust level.
- `POST /api/v1/memories` — REST write (mirrors `memclaw_write` over MCP).
- `mcp://…/mcp/` — streamable-http MCP endpoint.
