# @caura/memclaw-client

Official TypeScript/JavaScript client for [Caura](https://caura.ai) —
governed shared memory for AI agent fleets (multi-agent, multi-tenant,
MCP-native).

A thin wrapper over the Caura REST API. Point it at a managed
(`https://caura.ai`) or self-hosted (`http://localhost:8000`) deployment.
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

### Unknown fields on writes are rejected

`write()` spreads any unrecognised option into the request body. The API
rejects a field it does not declare with **422**, naming it in
`error.details.unknown_fields`:

```ts
// `tags` is not a write field — this throws CauraApiError (422).
await mc.write("a memory", { tags: ["alpha"] } as any);

// Caller-owned keys belong under `metadata`.
await mc.write("a memory", { metadata: { tags: ["alpha"] } });
```

This used to return `201` with the field silently discarded, so an integration
that "worked" may start failing here — the data it sent was never being stored.
`search()` and `recall()` are unaffected: filter bodies still accept unknown
fields, deliberately. See
[api-surfaces.md](https://github.com/caura-ai/caura/blob/main/docs/api-surfaces.md#request-body-contract-writes-are-strict-searches-are-not).

For credentials, scopes, and the full API surface, see the
[Caura docs](https://caura.ai/docs). Production fleets should use
[per-agent keys](https://caura.ai/docs/integrations/per-agent-keys).

## License

Apache-2.0
