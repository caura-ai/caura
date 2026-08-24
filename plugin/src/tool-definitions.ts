/**
 * Caura tool definitions — current surface (10 tools).
 *
 * One `createToolFromSpec(name)` factory wires together three sources:
 *
 *   - Name, label, description, plugin_exposed     ← `plugin/tools.json` (SoT)
 *   - Parameter JSON Schema                        ← `PARAM_SCHEMAS` below
 *   - HTTP dispatch (method/URL/body/validation)   ← `ENDPOINT_DISPATCH` below
 *
 * Op-dispatched tools (caura_manage, caura_doc) branch inside their
 * dispatch entry on `params.op`. Tool descriptions come from the
 * server's SoT registry (via `/tool-descriptions` →
 * `getToolDescription`), falling back to the description baked into
 * `tools.json` until the live fetch completes.
 *
 * Security properties preserved:
 * - UUID/safe-ID validation on all path-interpolated parameters
 * - encodeURIComponent on all ID path segments
 * - Signal forwarding to apiCall
 */

import { randomUUID } from "crypto";
import { apiCall, textResult } from "./transport.js";
import {
  MEMCLAW_FLEET_ID,
  MEMCLAW_AGENT_ID,
  ensureTenantId,
  getToolDescription,
} from "./env.js";
import { assertSafePathSegment } from "./validation.js";
import { getSpec } from "./tool-specs.js";
import { getInstallId } from "./install-id.js";

interface ToolResult {
  content: Array<{ type: string; text: string }>;
  details: Record<string, unknown>;
}

export interface AgentTool {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  execute(
    toolCallId: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<ToolResult>;
}

// --- Helpers ---

interface EnrichOptions {
  /**
   * Set on the tools whose ``fleet_id`` is *only* a read filter, so an
   * explicit ``scope: "all"`` is honoured instead of being narrowed back to
   * the configured fleet.
   *
   * ``caura_list`` and ``caura_stats`` are the two. Server-side they apply
   * ``fleet_id`` as a row filter OUTSIDE any scope branch
   * (``memory_list_by_filters`` / ``memory_stats_breakdown``:
   * ``if fleet_id: Memory.fleet_id == fleet_id``), and their shared ladder
   * ``resolve_read_fleet_gate`` passes it through untouched for 'all'. So a
   * defaulted ``fleet_id`` turns a tenant-wide read into a single-fleet one —
   * and being a strict equality it drops fleet-less (``NULL``) rows too, with
   * nothing in the response to signal it.
   *
   * NOT set on ``caura_insights`` / ``caura_evolve``, which also take
   * ``scope: "all"``. Their reads branch on ``scope`` and ignore ``fleet_id``
   * entirely under 'all' (``_insights_scope_filters`` /
   * ``evolve_service._filter_by_scope``), so there is no read to widen — and
   * for them ``fleet_id`` doubles as a WRITE target: the fleet persisted
   * findings and outcome rules land in, and the key insights supersedes priors
   * under. Withholding it there relocates a write instead of widening a read.
   */
  fleetIsReadFilterOnly?: boolean;
}

async function enrichBody(
  params: Record<string, unknown>,
  opts: EnrichOptions = {},
): Promise<Record<string, unknown>> {
  const body = { ...params };
  if (!body.tenant_id) body.tenant_id = await ensureTenantId();
  if (!body.agent_id && MEMCLAW_AGENT_ID) body.agent_id = MEMCLAW_AGENT_ID;
  // The configured fleet below is a DEFAULT, for callers that did not say
  // which fleet they meant — so it must not override a caller that asked to
  // span every fleet. Three cases deliberately keep it: an omitted ``scope``
  // (that default is what makes ordinary calls fleet-scoped in the first
  // place), ``scope='agent'`` / ``'fleet'`` (a fleet-scoped read is what they
  // ask for), and a caller-supplied ``fleet_id`` — this withholds a default,
  // it never strips a value.
  if (opts.fleetIsReadFilterOnly && body.scope === "all") return body;
  if (!body.fleet_id && MEMCLAW_FLEET_ID) body.fleet_id = MEMCLAW_FLEET_ID;
  return body;
}

function labelFor(name: string): string {
  const rest = name.replace(/^caura_?/, "");
  const titled = rest
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
  return titled ? `Caura ${titled}` : "Caura";
}

// Keep in sync with core-api/src/core_api/constants.py::MEMORY_TYPES
export const MEMORY_TYPES = [
  "fact", "episode", "decision", "preference", "task", "semantic",
  "intention", "plan", "commitment", "action", "outcome", "cancellation", "rule", "insight",
] as const;

export const STATUSES = [
  "active", "pending", "confirmed", "cancelled",
  "outdated", "conflicted", "archived", "deleted",
] as const;

const MEMORY_TYPE_SCHEMA = {
  type: "string",
  enum: [...MEMORY_TYPES],
  description: "Optional — auto-classified if omitted",
};

const STATUS_SCHEMA = {
  type: "string",
  enum: [...STATUSES],
  description: "Optional status",
};

// --- Parameter JSON Schemas ---

const PARAM_SCHEMAS: Record<string, Record<string, unknown>> = {
  caura_recall: {
    type: "object",
    required: ["query"],
    properties: {
      query: { type: "string", description: "Natural-language query (hybrid semantic+keyword)" },
      agent_id: { type: "string", description: "Caller agent ID for visibility scoping" },
      filter_agent_id: { type: "string", description: "Restrict to memories by this author" },
      memory_type: MEMORY_TYPE_SCHEMA,
      status: STATUS_SCHEMA,
      fleet_ids: { type: "array", items: { type: "string" }, description: "Restrict to fleets" },
      include_brief: { type: "boolean", description: "Append LLM-synthesized summary paragraph" },
      top_k: { type: "integer", description: "Max results (1-20)" },
    },
  },

  caura_write: {
    type: "object",
    required: ["agent_id"],
    properties: {
      agent_id: { type: "string", description: "REQUIRED. Your agent identifier." },
      content: { type: "string", description: "Single-write content. Provide one of {content, items}." },
      items: {
        type: "array", minItems: 1, maxItems: 100,
        description: "Batch of memory objects (provide one of {content, items}).",
        items: {
          type: "object", required: ["content"],
          properties: {
            content: { type: "string" },
            memory_type: MEMORY_TYPE_SCHEMA,
            weight: { type: "number" },
            source_uri: { type: "string" },
            run_id: { type: "string" },
            metadata: { type: "object" },
            status: STATUS_SCHEMA,
          },
        },
      },
      fleet_id: { type: "string", description: "Fleet scope" },
      visibility: { type: "string", enum: ["scope_agent", "scope_team", "scope_org"] },
      memory_type: MEMORY_TYPE_SCHEMA,
      weight: { type: "number", description: "Importance 0-1 (single-write only)" },
      source_uri: { type: "string", description: "Provenance URI (single-write only)" },
      run_id: { type: "string", description: "Run/session identifier (single-write only)" },
      metadata: { type: "object", description: "Additional metadata (single-write only)" },
      status: STATUS_SCHEMA,
      write_mode: { type: "string", enum: ["fast", "strong", "auto"], description: "Single-write only" },
    },
  },

  caura_manage: {
    type: "object",
    required: ["op", "memory_id"],
    properties: {
      op: { type: "string", enum: ["read", "update", "transition", "delete"] },
      memory_id: { type: "string", description: "UUID of memory to act on" },
      status: { type: "string", enum: [...STATUSES], description: "Required for op=transition" },
      content: { type: "string", description: "For op=update" },
      memory_type: MEMORY_TYPE_SCHEMA,
      weight: { type: "number", description: "For op=update (0-1)" },
      title: { type: "string", description: "For op=update" },
      metadata: { type: "object", description: "For op=update (replaces dict)" },
      source_uri: { type: "string", description: "For op=update" },
      agent_id: { type: "string", description: "Caller agent ID" },
    },
  },

  caura_doc: {
    type: "object",
    required: ["op"],
    properties: {
      op: {
        type: "string",
        enum: ["write", "read", "query", "delete", "search", "list_collections"],
      },
      collection: {
        type: "string",
        description:
          "Collection (table). Required for write|read|query|delete; " +
          "optional for search (omit to search every collection in the tenant) and list_collections.",
      },
      doc_id: { type: "string", description: "Required for op=write|read|delete" },
      data: {
        type: "object",
        description:
          "Required for op=write. JSON object with the document body — agent-defined keys " +
          "(e.g. {name, description, content} for skills, or any shape for custom collections).",
        // ``additionalProperties: true`` is JSON Schema's default but OpenClaw's
        // gateway-side AJV validator runs in strict mode (which silently flips
        // it to false for any object schema lacking explicit ``properties``).
        // Without this, every plugin-routed ``caura_doc op=write`` call from
        // an agent fails with "data: must not have additional properties" —
        // surfaced wet-testing the Phase B skill-share flow on memclaw.dev
        // (2026-05-06).
        additionalProperties: true,
      },
      where: { type: "object", description: "For op=query — field equality filters" },
      order_by: { type: "string", description: "For op=query" },
      order: { type: "string", enum: ["asc", "desc"], description: "For op=query" },
      limit: { type: "integer", description: "For op=query" },
      offset: { type: "integer", description: "For op=query" },
      agent_id: { type: "string" },
      fleet_id: { type: "string", description: "For op=write" },
      query: { type: "string", description: "op=search: natural-language query." },
      top_k: { type: "integer", description: "op=search: max results (1-50)." },
    },
  },

  caura_list: {
    type: "object",
    required: [],
    properties: {
      agent_id: { type: "string", description: "Caller agent ID (trust + visibility scoping)" },
      // Deliberately NO schema ``default``. ``GET /memories`` declares
      // ``scope`` as an opt-in query param (``Query(default=None)``), and an
      // omitted one skips the trust ladder and takes its author filter from
      // ``written_by ?? agent_id``. Declaring 'agent' here would make a
      // schema-honouring client start sending it, which adds the trust-1 gate
      // and, on an install with no agent id, turns a working tenant-wide
      // team/org read into a 400 ("scope='agent' requires an agent identity").
      // The two requests are near-identical once an agent id is configured,
      // but they are not the same request — so the description says what
      // omitting does instead of calling either one the default.
      // ``caura_stats`` below has the same shape.
      scope: { type: "string", enum: ["agent", "fleet", "all"], description: "Optional. Omitted: filtered by agent_id if one is set, with no trust gate — not the same request as 'agent'. 'agent' = your memories only (trust ≥ 1). 'fleet'/'all' = cross-agent (trust ≥ 2)." },
      fleet_id: { type: "string", description: "Restrict to a fleet" },
      written_by: { type: "string", description: "Filter by author agent_id. With scope='agent' it must be omitted or match your own agent_id — a different author is rejected, not ignored." },
      memory_type: MEMORY_TYPE_SCHEMA,
      status: STATUS_SCHEMA,
      weight_min: { type: "number" },
      weight_max: { type: "number" },
      created_after: { type: "string", format: "date-time" },
      created_before: { type: "string", format: "date-time" },
      sort: { type: "string", enum: ["created_at", "weight", "recall_count"] },
      order: { type: "string", enum: ["asc", "desc"] },
      limit: { type: "integer", description: "1-50" },
      cursor: { type: "string", description: "Opaque pagination cursor" },
      include_deleted: { type: "boolean", description: "Trust-3 only" },
    },
  },

  caura_entity_get: {
    type: "object",
    required: ["entity_id"],
    properties: {
      entity_id: { type: "string", description: "UUID of the entity to look up" },
    },
  },

  caura_tune: {
    type: "object",
    required: [],
    properties: {
      top_k: { type: "integer", description: "Max results per search (1-20)" },
      min_similarity: { type: "number", description: "Min similarity threshold (0.1-0.9)" },
      fts_weight: { type: "number", description: "Keyword vs semantic blend (0=semantic, 1=keyword)" },
      freshness_floor: { type: "number" },
      freshness_decay_days: { type: "integer" },
      recall_boost_cap: { type: "number" },
      recall_decay_window_days: { type: "integer" },
      graph_max_hops: { type: "integer", description: "Graph expansion depth (0-3)" },
      similarity_blend: { type: "number" },
    },
  },

  caura_insights: {
    type: "object",
    required: ["focus"],
    properties: {
      focus: {
        type: "string",
        enum: ["contradictions", "failures", "stale", "divergence", "patterns", "discover"],
        description: "Analysis focus mode",
      },
      scope: { type: "string", enum: ["agent", "fleet", "all"], description: "Scope of analysis" },
      fleet_id: { type: "string", description: "Required when scope='fleet'" },
      agent_id: { type: "string", description: "Caller agent" },
    },
  },

  caura_evolve: {
    type: "object",
    required: ["outcome", "outcome_type"],
    properties: {
      outcome: { type: "string", description: "What happened — natural language" },
      outcome_type: { type: "string", enum: ["success", "failure", "partial"] },
      related_ids: { type: "array", items: { type: "string" }, description: "Memory UUIDs that influenced the action" },
      scope: {
        type: "string",
        enum: ["agent", "fleet", "all"],
        description: "agent (default, trust ≥ 1, caller-owned memories) | fleet (trust ≥ 2) | all (trust ≥ 2)",
      },
      agent_id: { type: "string", description: "Caller agent" },
      fleet_id: { type: "string", description: "Required when scope='fleet'" },
    },
  },

  caura_stats: {
    type: "object",
    required: [],
    properties: {
      // No schema ``default`` — see the note on ``caura_list.scope`` above.
      scope: { type: "string", enum: ["agent", "fleet", "all"], description: "Optional. Omitted: aggregated over agent_id if one is set, with no trust gate — not the same request as 'agent'. 'agent' = only memories visible to you (trust ≥ 1). 'fleet'/'all' = cross-agent (trust ≥ 2)." },
      agent_id: { type: "string", description: "Caller agent ID" },
      fleet_id: { type: "string", description: "Restrict aggregate to a fleet" },
      memory_type: MEMORY_TYPE_SCHEMA,
      status: STATUS_SCHEMA,
    },
  },

  caura_keystones: {
    type: "object",
    required: [],
    properties: {
      agent_id: { type: "string", description: "Caller agent ID (used with fleet_id to include agent-scope rules)" },
      fleet_id: { type: "string", description: "Scope filter; supply to include fleet- and agent-scoped rules" },
    },
  },

};

// --- HTTP dispatch ---

type ExecuteFn = (
  params: Record<string, unknown>,
  signal?: AbortSignal,
) => Promise<unknown>;

// Translate friendly MCP-tool param names to existing REST query/body fields.
function searchBody(params: Record<string, unknown>): Record<string, unknown> {
  const body: Record<string, unknown> = { ...params };
  if (body.memory_type !== undefined) {
    body.memory_type_filter = body.memory_type;
    delete body.memory_type;
  }
  if (body.status !== undefined) {
    body.status_filter = body.status;
    delete body.status;
  }
  delete body.include_brief;
  return body;
}

const ENDPOINT_DISPATCH: Record<string, ExecuteFn> = {
  caura_recall: async (params, signal) => {
    const body = await enrichBody(searchBody(params));
    const includeBrief = Boolean(params.include_brief);
    const results = await apiCall("POST", "/search", body, undefined, signal);
    if (!includeBrief) return { results };
    const brief = await apiCall("POST", "/recall", body, undefined, signal);
    return { results, brief };
  },

  caura_write: async (params, signal) => {
    const isBatch = Array.isArray(params.items);
    const body = await enrichBody(params);
    // Write-scoped identity default: never send an empty agent_id, which on the
    // gateway path collapses onto the reserved "main" default. enrichBody set it
    // from MEMCLAW_AGENT_ID if present; otherwise fall back to a stable
    // install-scoped id (mirrors resolve-agent.ts step 5). Scoped to writes so
    // read visibility scoping is unchanged.
    if (!body.agent_id) body.agent_id = `main-${getInstallId()}`;
    if (isBatch) {
      // POST /memories/bulk requires a per-attempt idempotency token via
      // the `X-Bulk-Attempt-Id` header (CAURA-602). The server derives each
      // row's `client_request_id` from `${X-Bulk-Attempt-Id}:${index}`, so a
      // retry with the same id resolves committed rows as `duplicate_attempt`
      // instead of duplicating. Omitting it makes the endpoint return
      // HTTP 400 "Missing required X-Bulk-Attempt-Id header" — which is why
      // batch writes failed entirely. Generate one UUID per tool invocation
      // (a 401-retry inside apiCall reuses it), mirroring the server's own
      // MCP path which auto-generates `mcp:{uuid4()}`.
      return apiCall("POST", "/memories/bulk", body, undefined, signal, undefined, {
        "X-Bulk-Attempt-Id": randomUUID(),
      });
    }
    return apiCall("POST", "/memories", body, undefined, signal);
  },

  caura_manage: async (params, signal) => {
    const enriched = await enrichBody(params);
    const op = enriched.op as string;
    const memory_id = enriched.memory_id as string;
    assertSafePathSegment(memory_id, "memory_id");
    const tenant_id = enriched.tenant_id as string;
    const id = encodeURIComponent(memory_id);
    if (op === "read") {
      return apiCall("GET", `/memories/${id}`, undefined, { tenant_id }, signal);
    }
    if (op === "transition") {
      return apiCall(
        "PATCH",
        `/memories/${id}/status`,
        { status: enriched.status },
        { tenant_id },
        signal,
      );
    }
    if (op === "delete") {
      return apiCall("DELETE", `/memories/${id}`, undefined, { tenant_id }, signal);
    }
    // op === "update"
    const agent_id = (enriched.agent_id as string) || "unknown-agent";
    const updateFields: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined) continue;
      if (k === "op" || k === "memory_id" || k === "tenant_id" || k === "agent_id" || k === "fleet_id") continue;
      updateFields[k] = v;
    }
    return apiCall(
      "PATCH",
      `/memories/${id}`,
      updateFields,
      { tenant_id, agent_id },
      signal,
    );
  },

  caura_doc: async (params, signal) => {
    const enriched = await enrichBody(params);
    const op = enriched.op as string;
    const collection = enriched.collection as string | undefined;
    const tenant_id = enriched.tenant_id as string;
    if (op === "write") {
      return apiCall("POST", "/documents", {
        tenant_id,
        collection,
        doc_id: enriched.doc_id,
        data: enriched.data,
        fleet_id: enriched.fleet_id,
        agent_id: enriched.agent_id,
      }, undefined, signal);
    }
    if (op === "read") {
      return apiCall(
        "GET",
        `/documents/${encodeURIComponent(enriched.doc_id as string)}`,
        undefined,
        { tenant_id, collection: collection as string },
        signal,
      );
    }
    if (op === "query") {
      const body: Record<string, unknown> = {
        tenant_id,
        collection,
        where: enriched.where ?? {},
        order_by: enriched.order_by,
        order: enriched.order,
        limit: enriched.limit,
        offset: enriched.offset,
        fleet_id: enriched.fleet_id,
      };
      return apiCall("POST", "/documents/query", body, undefined, signal);
    }
    if (op === "search") {
      const body: Record<string, unknown> = {
        tenant_id,
        collection,
        query: enriched.query,
        top_k: enriched.top_k ?? 5,
        fleet_id: enriched.fleet_id,
      };
      return apiCall("POST", "/documents/search", body, undefined, signal);
    }
    if (op === "list_collections") {
      const query: Record<string, string> = { tenant_id };
      if (enriched.fleet_id) query.fleet_id = String(enriched.fleet_id);
      return apiCall("GET", "/documents/collections", undefined, query, signal);
    }
    // op === "delete"
    return apiCall(
      "DELETE",
      `/documents/${encodeURIComponent(enriched.doc_id as string)}`,
      undefined,
      { tenant_id, collection: collection as string },
      signal,
    );
  },

  caura_list: async (params, signal) => {
    const enriched = await enrichBody(params, { fleetIsReadFilterOnly: true });
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined || v === null) continue;
      query[k] = String(v);
    }
    return apiCall("GET", "/memories", undefined, query, signal);
  },

  caura_entity_get: async (params, signal) => {
    const enriched = await enrichBody(params);
    const entity_id = enriched.entity_id as string;
    assertSafePathSegment(entity_id, "entity_id");
    const tenant_id = enriched.tenant_id as string;
    return apiCall(
      "GET",
      `/entities/${encodeURIComponent(entity_id)}`,
      undefined,
      { tenant_id },
      signal,
    );
  },

  caura_tune: async (params, signal) => {
    const enriched = await enrichBody(params);
    const tenant_id = enriched.tenant_id as string;
    const agent_id = (enriched.agent_id as string) || "unknown-agent";
    assertSafePathSegment(agent_id, "agent_id");
    const body = { ...enriched };
    delete body.agent_id;
    delete body.tenant_id;
    delete body.fleet_id;
    return apiCall(
      "PATCH",
      `/agents/${encodeURIComponent(agent_id)}/tune`,
      body,
      { tenant_id },
      signal,
      agent_id,  // explicit: agent_id was removed from body
    );
  },

  caura_insights: async (params, signal) => {
    const body = await enrichBody(params);
    return apiCall("POST", "/insights/generate", body, undefined, signal);
  },

  caura_evolve: async (params, signal) => {
    const body = await enrichBody(params);
    return apiCall("POST", "/evolve/report", body, undefined, signal);
  },

  caura_stats: async (params, signal) => {
    const enriched = await enrichBody(params, { fleetIsReadFilterOnly: true });
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined || v === null) continue;
      query[k] = String(v);
    }
    return apiCall("GET", "/memories/stats", undefined, query, signal);
  },

  // GET /api/v1/memclaw/keystones — read-only; trust gate is open (PR3).
  // The plugin's ContextEngine fetches this at session start and prepends
  // the result to the system prompt (see ``plugin/src/keystones.ts``).
  // Exposing it as a callable tool too gives agents a way to re-fetch
  // mid-session when they suspect rules have changed.
  caura_keystones: async (params, signal) => {
    const enriched = await enrichBody(params);
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined || v === null) continue;
      // agent-scope rows are keyed on (fleet_id, agent_id) — sending
      // agent_id without fleet_id would silently degrade the result
      // and produce a different rule set from the one the agent saw
      // injected at session start. Mirrors the auto-inject guard in
      // ``plugin/src/keystones.ts``.
      if (k === "agent_id" && !enriched.fleet_id) continue;
      query[k] = String(v);
    }
    return apiCall("GET", "/memclaw/keystones", undefined, query, signal);
  },

};

// --- Factory ---

/**
 * Build a registered `AgentTool` by name.
 *
 * Throws at construction if the tool is missing a parameters schema or
 * dispatch entry — a sanity check to catch local drift between
 * `PARAM_SCHEMAS`, `ENDPOINT_DISPATCH`, and `tools.json`.
 */
export function createToolFromSpec(name: string): AgentTool {
  const spec = getSpec(name);
  const parameters = PARAM_SCHEMAS[name];
  const execute = ENDPOINT_DISPATCH[name];
  if (!parameters) {
    throw new Error(`[caura] Missing PARAM_SCHEMAS entry for '${name}'`);
  }
  if (!execute) {
    throw new Error(`[caura] Missing ENDPOINT_DISPATCH entry for '${name}'`);
  }
  const label = labelFor(name);
  const fallbackDescription = spec.description;
  return {
    name: spec.name,
    label,
    get description() {
      return getToolDescription(spec.name, fallbackDescription);
    },
    parameters,
    async execute(_toolCallId, params, signal) {
      const result = await execute(params, signal);
      return textResult(JSON.stringify(result, null, 2));
    },
  };
}
