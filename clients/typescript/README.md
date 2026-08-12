# @caura/memclaw-client

Official TypeScript/JavaScript client for [Caura](https://memclaw.net) —
governed shared memory for AI agent fleets (multi-agent, multi-tenant,
MCP-native).

A thin wrapper over the Caura REST API. Point it at a managed
(`https://memclaw.net`) or self-hosted (`http://localhost:8000`) deployment.
Zero runtime dependencies — uses native `fetch` (Node 18+).

## Install

```bash
npm install @caura/memclaw-client
```

## Quickstart

```ts
import { Caura } from "@caura/memclaw-client";

const mc = new Caura("mc_xxx", { tenantId: "my-team", agentId: "my-agent" });

// Write a memory — enriched server-side with type, title, tags, importance.
await mc.write("Q3 revenue target is $4M, set on 2026-04-15.");

// Search (ranked raw results)
for (const m of await mc.search("Q3 revenue target", { topK: 5 })) {
  console.log(m.title, "—", m.content);
}

// Recall (LLM-synthesized context brief)
console.log((await mc.recall("Q3 revenue target")).summary);
```

Self-hosted? Pass `baseUrl`:

```ts
const mc = new Caura("standalone", { tenantId: "default", baseUrl: "http://localhost:8000" });
```

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, opts?)` | `POST /api/v1/memories` | `Memory` |
| `search(query, opts?)` | `POST /api/v1/search` | `Memory[]` |
| `recall(query, opts?)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `object` |

Failures throw `AuthError` (401/403), `NotFoundError` (404), or
`CauraApiError`. Every result also exposes the full API payload on `.raw`.

For credentials, scopes, and the full API surface, see the
[Caura docs](https://memclaw.net/docs). Production fleets should use
[per-agent keys](https://memclaw.net/docs/integrations/per-agent-keys).

## License

Apache-2.0
