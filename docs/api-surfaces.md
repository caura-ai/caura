# API Surface Ownership Charter

Caura exposes three callable surfaces — REST, MCP, and the OpenClaw plugin.
They are intentionally **not symmetric**. Each serves a different audience and
operates under different assumptions. This document records who owns what and
when to add or move an operation.

> Looking for **latency and throughput numbers** per surface? See
> [`performance.md`](performance.md). This document is about *which surface
> owns which operation*, not about how fast each one runs.

## Audiences

| Surface | Primary audience | Trust model |
|---------|------------------|-------------|
| REST    | Web UI, ops dashboards, programmatic admins, scripted clients | Explicit `tenant_id` in body/query; API key + role gating; full operational vocabulary. |
| MCP     | AI agents (Claude Code, third-party agents) | Tenant inferred from auth header; agent-natural operations; description-driven discoverability. |
| OpenClaw plugin | Local plugin runtime that hosts agents | Operational concerns of the local install — heartbeat, agent registration, fleet readiness. Delegates business operations to MCP. |

## Surface ownership

| Concern | Owner | Reason |
|---------|-------|--------|
| Memory CRUD (write/recall/list/get/update/delete/transition) | REST + MCP | Universal; needed by every audience. |
| Bulk write | REST + MCP | Same. |
| Per-agent search profile (tune) | REST + MCP | Both administrators (REST) and agents tuning themselves (MCP) need it. |
| Doc CRUD | REST + MCP | Universal. |
| Doc semantic search | MCP **and** REST | Was MCP-only; REST endpoint added so non-MCP clients can use vector search on documents. |
| Doc list-collections | MCP **and** REST | Was MCP-only; REST endpoint added for parity with list use cases. |
| Bulk delete | REST + MCP (`caura_manage op=bulk_delete`) | Admin sometimes; agents cleaning up after themselves sometimes. |
| Memory lineage walk | REST + MCP (`caura_manage op=lineage`) | Agents reviewing their own writes need to trace supersession chains. |
| Knowledge graph / `/graph` | REST only | Aggregation surface for UIs and analytics tools. Agents that need entity context use `caura_entity_get` (single entity) and `caura_recall` (with entity_links in results). |
| Memory stats | REST + MCP (`caura_stats`) | Aggregate counts (total + breakdown by type, agent, status; opt-in `include_deleted=true` adds `deleted` and `total_including_deleted`) — useful for admin/dashboard usage on REST and for agent self-introspection on MCP. Read-only aggregations don't need a use-case gate. |
| Skill sharing | REST (`/documents` + `/documents/search` on `collection="skills"`) + MCP (`caura_doc op=write\|read\|query\|delete\|search collection=skills`) | Skill sharing rides the generic document surface. Slugs (`doc_id`) are constrained to `^[a-z0-9][a-z0-9._-]{0,99}$` (filesystem-safe), and skills writes require `data["summary"]` (with back-compat fallback to `data["description"]`) so the catalog is semantic-searchable without ceremony. The dedicated `memclaw_share_skill`/`memclaw_unshare_skill` <!-- legacy-name-floor: names the removed share/unshare tools --> tools and `/skills/*` REST routes were dropped 2026-05; fleet auto-install (push to every node) is restored by Phase A's plugin-side reconciler. Trust ≥ 1 (inherited from `caura_doc`). |
| Keystones (mandatory rules) | REST (`/keystones`; permanent legacy alias `/memclaw/keystones` <!-- legacy-name-ok: taught as legacy alias -->) + MCP (`caura_keystones` read, `caura_keystones_set` set\|delete) | Governance policies that agents MUST obey — fetched deterministically (no semantic search) and injected into every session by the OpenClaw plugin. Storage lives in the system-managed `_keystones` collection on `documents`; the dedicated surface exists so the read tool stays discoverable in MCP `instructions` and the write surface can be trust-gated separately. Reads are open. Writes are tiered: a freshly-registered (trust ≥ 1) agent can author its own rule — `scope=agent` carrying an explicit `agent_id` equal to the caller, i.e. self-authored autonomy — but `scope=fleet`, `scope=tenant`, cross-agent `scope=agent`, and `scope=agent` with `agent_id` omitted all stay at trust ≥ 2 so a default-trust agent (or a prompt-injected one) can't plant a tenant-wide rule. The explicit `agent_id` is the precondition for the self-author tier: a payload that names no target agent isn't self-authored, so it gets the ≥ 2 bar (and storage rejects the shape anyway — "scope=agent requires agent_id"). |
| Tenant settings | REST only | Settings are a tenant-administrator concern; not safe for arbitrary agents to flip global config. |
| Redistribute (mass reassign) | REST only | Destructive bulk op requires `trust_level >= 3`. Admin operation, not agent-driven. |
| Ingest preview/commit | REST only (revisit per use case) | Pipeline workflow; expose to MCP only if "agent crawls a URL and writes memories" becomes a real use case. |
| Contradictions / lineage at `/memories/{id}/contradictions` | REST + MCP (`caura_manage op=lineage`) | UI gets the rich endpoint; agents get the focused tool. |
| Heartbeat / fleet readiness | REST receives, plugin produces | Plugin is the natural producer (it knows the local node state). REST is the receiver. MCP doesn't need this — agents don't report their own runtime state. |
| Agent registration | OpenClaw plugin | Local-runtime concern. |

## When to add an operation

Before adding a new MCP tool that mirrors a REST endpoint, justify it with a
concrete agent workflow OR demonstrate that the operation is a read-only
aggregation/introspection:

1. **Read-only aggregations** (counts, summaries, listings of caller-visible
   state) don't need a blocking use case — they're cheap, safe, and useful for
   any agent that wants to introspect the store. Add freely; trust gate at
   the same level as `caura_list` (≥ 1 for own scope, ≥ 2 for cross-agent).
2. For everything else: is there an agent that **today** is blocked from
   doing useful work because this operation only exists on REST?
3. If yes, does it fit naturally into an existing tool (`caura_manage`,
   `caura_doc`, etc.) as another `op=...`? Prefer extending an existing
   tool over adding a new top-level surface.
4. If a new tool is genuinely warranted, it must include: a clear description,
   trust-level enforcement consistent with the REST counterpart, and a wet
   test that exercises the same workflow against the local docker stack.

Before adding a new REST endpoint that mirrors an MCP tool, justify it with a
concrete non-agent workflow (a UI screen, a script, an integration). Don't
mirror "for symmetry."

## What this charter is NOT

- It's not a list of bugs. Asymmetry is the design, not a defect.
- It's not exhaustive — new operations should be classified above when added.
- It's not a justification for current cross-surface drift in error contracts
  or response shapes — those are separate hygiene concerns and should be
  fixed regardless of where each operation lives.

## Cross-surface hygiene (separate from ownership)

The following inconsistencies span surfaces and should be addressed
independently of ownership decisions:

- **Error contracts**: largely closed. Both surfaces now emit the canonical
  `{"error": {"code", "message", "details"?}}` envelope from
  `core_api.errors.make_error_payload` — REST alongside the legacy
  top-level `detail`, MCP as the tool's JSON string inside a
  `CallToolResult(isError=True)`. What remains is the transport: REST
  carries the HTTP status, MCP has only the `code`, so a cross-surface
  client still branches on one or the other.
- **Response shape drift on `recall`**: REST `/recall` returns
  `{query, summary, memory_count, memories, items, recall_ms}`; MCP
  `caura_recall(include_brief=true)` returns
  `{results, items, count, brief: <REST-recall-response>}`. Both dual-emit
  the row list under `items` now, so that key is the safe one to read on
  either surface — but the rest of the envelope (and the nesting of the
  brief) still differs for one conceptual operation. Pick one and align.
  `summary` behaves the same on both: the model is prompted to reason step
  by step and to close with a `**Answer:**` line, and the server surfaces
  only that final answer — callers get the answer, not the scaffold, and
  never have to parse the marker themselves. If a completion carries no
  marker (no-LLM fallback, truncation, a model that ignored the format),
  the full completion is returned unchanged.
- **Tenant resolution**: REST takes `body.tenant_id`; MCP infers from auth
  header. Both are reasonable; document the convention.

---

## Request-body contract: writes are strict, searches are not

This is a deliberate asymmetry, and the one place it is written down.

**Write bodies reject fields they do not declare.** `POST /api/v1/memories`,
`POST /api/v1/documents`, `PATCH /api/v1/memories/{id}` and every other
write/mutation endpoint respond **422** to an unrecognised key, naming it:

```json
{
  "error": {
    "code": "INVALID_ARGUMENTS",
    "message": "unknown field 'contnet' is not permitted on this request body (at 'contnet')",
    "details": { "unknown_fields": ["contnet"] }
  }
}
```

`error.details.unknown_fields` carries the offending names as dotted paths, so
a nested one reads `facts.0.saliance`. The back-compat `detail` array still
carries pydantic's verbatim entries, with the field in `loc`.

Until this changed, an undeclared key was **silently discarded**: a write with
a misspelled field returned `201 Created` and stored the row without it. The
caller was told it had succeeded. That is worse than a 422, because there is
nothing to notice.

**Search, filter and query bodies still accept unknown fields.**
`POST /api/v1/search`, `/api/v1/recall`, `/api/v1/documents/query` and
`/api/v1/documents/search` ignore what they do not recognise, and will keep
doing so. The two cases are not symmetric:

| | unknown field on a **write** | unknown field on a **search** |
|---|---|---|
| What breaks | stored data is missing what the caller sent | the result set is wider than intended |
| Can the caller see it? | no — the response says `201` | yes — the results are visibly wrong |
| Recoverable after the fact? | only by re-writing, if anyone notices | re-run the query |

Search bodies also carry historical spellings absorbed by `AliasChoices`
(`memory_type` ↔ `memory_type_filter`, `status` ↔ `status_filter` — the C1+C2
incident). Those are a standing compatibility promise, not an oversight.

### Two write bodies are deliberately permissive

- **`POST /api/v1/memories/bulk` items.** The *envelope* is strict, but an
  unknown key inside one `items[]` entry is reported as that item's own
  `status="error"` row in the 207 response — not as a 422 for the batch. One
  item's typo must not discard its valid siblings.
- **`POST /api/v1/fleet/heartbeat`** and **`POST /api/v1/fleet/commands/{id}/result`.**
  Plugin-produced telemetry, and there is no version handshake between plugin
  and backend (`RELEASING.md` § Compatibility). The body carries nothing the
  caller owns, and rejecting it would take the node offline in fleet views and
  cut the command channel that carries its own upgrade.

### For integrators

If you send a field the API does not document, you will now get a 422 where
you used to get a 2xx. That is the point — the field was never being stored —
but it is a behaviour change. Check write payloads against the OpenAPI schema
(`/openapi.json`); anything not in a request model's `properties` was already
being thrown away.
