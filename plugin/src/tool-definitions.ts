/**
 * MemClaw tool definitions — current surface (10 tools).
 *
 * One `createToolFromSpec(name)` factory wires together three sources:
 *
 *   - Name, label, description, plugin_exposed     ← `plugin/tools.json` (SoT)
 *   - Parameter JSON Schema                        ← `PARAM_SCHEMAS` below
 *   - HTTP dispatch (method/URL/body/validation)   ← `ENDPOINT_DISPATCH` below
 *
 * Op-dispatched tools (memclaw_manage, memclaw_doc) branch inside their
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

import { apiCall, textResult } from "./transport.js";
import {
  MEMCLAW_FLEET_ID,
  MEMCLAW_AGENT_ID,
  ensureTenantId,
  getToolDescription,
} from "./env.js";
import { assertSafePathSegment } from "./validation.js";
import { getSpec } from "./tool-specs.js";

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

async function enrichBody(
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const body = { ...params };
  if (!body.tenant_id) body.tenant_id = await ensureTenantId();
  if (!body.agent_id && MEMCLAW_AGENT_ID) body.agent_id = MEMCLAW_AGENT_ID;
  if (!body.fleet_id && MEMCLAW_FLEET_ID) body.fleet_id = MEMCLAW_FLEET_ID;
  return body;
}

function labelFor(name: string): string {
  const rest = name.replace(/^memclaw_?/, "");
  const titled = rest
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
  return titled ? `MemClaw ${titled}` : "MemClaw";
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
  memclaw_recall: {
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

  memclaw_write: {
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

  memclaw_manage: {
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

  memclaw_doc: {
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
        // Without this, every plugin-routed ``memclaw_doc op=write`` call from
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

  memclaw_list: {
    type: "object",
    required: [],
    properties: {
      agent_id: { type: "string", description: "Caller agent ID (trust + visibility scoping)" },
      scope: { type: "string", enum: ["agent", "fleet", "all"], description: "'agent' (default) = your memories only (trust ≥ 1). 'fleet'/'all' = cross-agent (trust ≥ 2)." },
      fleet_id: { type: "string", description: "Restrict to a fleet" },
      written_by: { type: "string", description: "Filter by author agent_id (ignored when scope='agent')" },
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

  memclaw_entity_get: {
    type: "object",
    required: ["entity_id"],
    properties: {
      entity_id: { type: "string", description: "UUID of the entity to look up" },
    },
  },

  memclaw_tune: {
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

  memclaw_insights: {
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

  memclaw_evolve: {
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

  memclaw_stats: {
    type: "object",
    required: [],
    properties: {
      scope: { type: "string", enum: ["agent", "fleet", "all"], description: "'agent' (default, trust ≥ 1) | 'fleet'/'all' (trust ≥ 2)" },
      agent_id: { type: "string", description: "Caller agent ID" },
      fleet_id: { type: "string", description: "Restrict aggregate to a fleet" },
      memory_type: MEMORY_TYPE_SCHEMA,
      status: STATUS_SCHEMA,
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
  memclaw_recall: async (params, signal) => {
    const body = await enrichBody(searchBody(params));
    const includeBrief = Boolean(params.include_brief);
    const results = await apiCall("POST", "/search", body, undefined, signal);
    if (!includeBrief) return { results };
    const brief = await apiCall("POST", "/recall", body, undefined, signal);
    return { results, brief };
  },

  memclaw_write: async (params, signal) => {
    const isBatch = Array.isArray(params.items);
    const body = await enrichBody(params);
    if (isBatch) return apiCall("POST", "/memories/bulk", body, undefined, signal);
    return apiCall("POST", "/memories", body, undefined, signal);
  },

  memclaw_manage: async (params, signal) => {
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

  memclaw_doc: async (params, signal) => {
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

  memclaw_list: async (params, signal) => {
    const enriched = await enrichBody(params);
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined || v === null) continue;
      query[k] = String(v);
    }
    return apiCall("GET", "/memories", undefined, query, signal);
  },

  memclaw_entity_get: async (params, signal) => {
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

  memclaw_tune: async (params, signal) => {
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

  memclaw_insights: async (params, signal) => {
    const body = await enrichBody(params);
    return apiCall("POST", "/insights/generate", body, undefined, signal);
  },

  memclaw_evolve: async (params, signal) => {
    const body = await enrichBody(params);
    return apiCall("POST", "/evolve/report", body, undefined, signal);
  },

  memclaw_stats: async (params, signal) => {
    const enriched = await enrichBody(params);
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(enriched)) {
      if (v === undefined || v === null) continue;
      query[k] = String(v);
    }
    return apiCall("GET", "/memories/stats", undefined, query, signal);
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
    throw new Error(`[memclaw] Missing PARAM_SCHEMAS entry for '${name}'`);
  }
  if (!execute) {
    throw new Error(`[memclaw] Missing ENDPOINT_DISPATCH entry for '${name}'`);
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
