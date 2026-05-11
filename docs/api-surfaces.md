# API Surface Ownership Charter

MemClaw exposes three callable surfaces — REST, MCP, and the OpenClaw plugin.
They are intentionally **not symmetric**. Each serves a different audience and
operates under different assumptions. This document records who owns what and
when to add or move an operation.

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
| Bulk delete | REST + MCP (`memclaw_manage op=bulk_delete`) | Admin sometimes; agents cleaning up after themselves sometimes. |
| Memory lineage walk | REST + MCP (`memclaw_manage op=lineage`) | Agents reviewing their own writes need to trace supersession chains. |
| Knowledge graph / `/graph` | REST only | Aggregation surface for UIs and analytics tools. Agents that need entity context use `memclaw_entity_get` (single entity) and `memclaw_recall` (with entity_links in results). |
| Memory stats | REST + MCP (`memclaw_stats`) | Aggregate counts (total + breakdown by type, agent, status; opt-in `include_deleted=true` adds `deleted` and `total_including_deleted`) — useful for admin/dashboard usage on REST and for agent self-introspection on MCP. Read-only aggregations don't need a use-case gate. |
| Skill sharing | REST (`/documents` + `/documents/search` on `collection="skills"`) + MCP (`memclaw_doc op=write\|read\|query\|delete\|search collection=skills`) | Skill sharing rides the generic document surface. Slugs (`doc_id`) are constrained to `^[a-z0-9][a-z0-9._-]{0,99}$` (filesystem-safe), and the server auto-defaults `embed_field=description` when `collection=skills` so the catalog is semantic-searchable without ceremony. The dedicated `memclaw_share_skill`/`memclaw_unshare_skill` tools and `/skills/*` REST routes were dropped 2026-05; fleet auto-install (push to every node) is restored by Phase A's plugin-side reconciler. Trust ≥ 1 (inherited from `memclaw_doc`). |
| Keystones (mandatory rules) | REST (`/memclaw/keystones`) + MCP (`memclaw_keystones` read, `memclaw_keystones_set` set\|delete) | Governance policies that agents MUST obey — fetched deterministically (no semantic search) and injected into every session by the OpenClaw plugin. Storage lives in the system-managed `_keystones` collection on `documents`; the dedicated surface exists so the read tool stays discoverable in MCP `instructions` and the write surface can be trust-gated (≥2, the elevated/cross-agent tier) separately. Reads are open; writes require trust ≥ 2 so a default-trust agent — including a prompt-injected newly-registered one — can't plant a tenant-wide rule. |
| Tenant settings | REST only | Settings are a tenant-administrator concern; not safe for arbitrary agents to flip global config. |
| Redistribute (mass reassign) | REST only | Destructive bulk op requires `trust_level >= 3`. Admin operation, not agent-driven. |
| Ingest preview/commit | REST only (revisit per use case) | Pipeline workflow; expose to MCP only if "agent crawls a URL and writes memories" becomes a real use case. |
| Contradictions / lineage at `/memories/{id}/contradictions` | REST + MCP (`memclaw_manage op=lineage`) | UI gets the rich endpoint; agents get the focused tool. |
| Heartbeat / fleet readiness | REST receives, plugin produces | Plugin is the natural producer (it knows the local node state). REST is the receiver. MCP doesn't need this — agents don't report their own runtime state. |
| Agent registration | OpenClaw plugin | Local-runtime concern. |

## When to add an operation

Before adding a new MCP tool that mirrors a REST endpoint, justify it with a
concrete agent workflow OR demonstrate that the operation is a read-only
aggregation/introspection:

1. **Read-only aggregations** (counts, summaries, listings of caller-visible
   state) don't need a blocking use case — they're cheap, safe, and useful for
   any agent that wants to introspect the store. Add freely; trust gate at
   the same level as `memclaw_list` (≥ 1 for own scope, ≥ 2 for cross-agent).
2. For everything else: is there an agent that **today** is blocked from
   doing useful work because this operation only exists on REST?
3. If yes, does it fit naturally into an existing tool (`memclaw_manage`,
   `memclaw_doc`, etc.) as another `op=...`? Prefer extending an existing
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

- **Error contracts**: REST raises `HTTPException` with status codes; MCP
  returns string error prefixes (`"Error (422): ..."`). Cross-surface clients
  must special-case. Pick one canonical shape and align both surfaces.
- **Response shape drift on `recall`**: REST `/recall` returns
  `{query, summary, memory_count, memories, recall_ms}`; MCP
  `memclaw_recall(include_brief=true)` returns
  `{results, brief: <REST-recall-response>}`. Same conceptual operation,
  different payloads. Pick one and align.
- **Tenant resolution**: REST takes `body.tenant_id`; MCP infers from auth
  header. Both are reasonable; document the convention.
