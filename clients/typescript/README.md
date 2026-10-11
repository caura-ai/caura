# @caura/client

Official TypeScript/JavaScript client for [Caura](https://caura.ai) —
governed shared memory for AI agent fleets (multi-agent, multi-tenant,
MCP-native).

A thin wrapper over the Caura REST API. Point it at a managed
(`https://caura.ai`) or self-hosted (`http://localhost:8000`) deployment.
Zero runtime dependencies — uses native `fetch` (Node 18+).

## Install

```bash
npm install @caura/client
```

## Quickstart

```ts
import { Caura } from "@caura/client";

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

Plain `http://` sends the API key in clear, so the client allows it only to a
loopback host (`localhost`, `127.0.0.0/8`, `::1`) and otherwise throws, naming
the host. Use `https://`, or pass `allowInsecureHttp: true` (or set
`CAURA_ALLOW_INSECURE_HTTP=true`) to accept the risk, e.g. on a trusted private
network. Requests refuse redirects, so the key is never re-sent elsewhere: point
`baseUrl` at the final URL.

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, opts?)` | `POST /api/v1/memories` | `Memory` |
| `search(query, opts?)` | `POST /api/v1/search` | `SearchResult` (a `Memory[]`) |
| `recall(query, opts?)` | `POST /api/v1/recall` | `RecallResult` |
| `getDocument(docId, opts)` | `GET /api/v1/documents/{docId}` | `object` |
| `health()` | `GET /api/v1/health` | `object` |

Failures throw `AuthError` (401/403), `NotFoundError` (404), or
`CauraApiError` for HTTP errors. Network failures and timeouts while awaiting
response headers or consuming the response body throw `TransportError`, with
the original rejection in `cause`. All extend `CauraError`, so one catch can
handle both HTTP and transport failures. Transport errors have no HTTP status
code; requests are not retried. Every result also exposes the full API payload
on `.raw`. `search()` resolves to a `SearchResult`: an array of `Memory` that
also carries the response's `recallTracked`, `diagnostic` (set by
`diagnostic: true`) and `warnings` (for example a parameter the server
ignored), and the whole body on `.raw`.

`recall()` reads the brief's `memories` and asks the server not to repeat them
under `items` (`items_alias: false`), which halves the response. Pass
`items_alias: true` if you read `raw.items`.

### Reading as an agent with a tenant key

With a tenant-scoped key, `search()` and `recall()` read as the tenant, so they
do not return any agent's `scope_agent` memories, including ones this client
wrote with `agentId`. Pass `callerAgentId` to read as that agent:

```ts
const mc = new Caura("mc_tenant_key", { tenantId: "my-team", agentId: "my-agent" });
const mine = await mc.search("deploy checklist", { callerAgentId: mc.agentId });
```

The server then treats the read as that agent's: it registers the agent if it
is new, and holds the read to the agent's fleet and trust level, so a trust-1
agent reads its own fleet and is refused another. It is opt-in, never sent from
`agentId` alone. An agent-scoped key already reads as its agent, and may only
name itself here.

### Fetching a document

`getDocument()` returns the full `DocOut` envelope — the stored record is
nested under the `"data"` key, not returned directly. `collection` is a
required option, and a missing document raises `NotFoundError`:

```ts
const doc = await mc.getDocument("doc-123", { collection: "interviews" });
const record = doc.data; // the stored record lives under "data"
```

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

## Request headers

Every request carries `X-API-Key` (your key), `Content-Type: application/json`
and a `User-Agent` of the form `caura-client-node/<version> (node/<major>)`.
The `User-Agent` lets a Caura server count which SDK families talk to it; it
names only the package, its version and the Node major (browsers drop the
header, which is fine). The client sends nothing to any host other than the
`baseUrl` you configure. The version is also exported as `VERSION`.

## Not `npm install caura`

The unscoped name is unavailable. npm's registry rejects it as too similar to
`csurf`, a long-established package, and that rejection is not appealable in
practice — the scoped name is the supported route rather than a workaround.

## License

Apache-2.0
