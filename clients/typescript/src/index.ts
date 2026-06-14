/**
 * Official TypeScript/JavaScript client for MemClaw — governed shared memory
 * for AI agent fleets. A thin wrapper over the MemClaw REST API.
 *
 * Point it at a managed (`https://memclaw.net`) or self-hosted
 * (`http://localhost:8000`) deployment.
 */

export const DEFAULT_BASE_URL = "https://memclaw.net";

export class MemClawError extends Error {}

export class MemClawApiError extends MemClawError {
  readonly statusCode: number;
  readonly details: unknown;
  constructor(statusCode: number, message: string, details?: unknown) {
    super(`[${statusCode}] ${message}`);
    this.name = "MemClawApiError";
    this.statusCode = statusCode;
    this.details = details;
  }
}

/** Raised on 401/403 — bad or insufficiently-scoped credential. */
export class AuthError extends MemClawApiError {}

/** Raised on 404. */
export class NotFoundError extends MemClawApiError {}

export interface Memory {
  id: string | null;
  content: string;
  title: string | null;
  memoryType: string | null;
  tenantId: string | null;
  agentId: string | null;
  weight: number | null;
  similarity: number | null;
  metadata: Record<string, unknown> | null;
  /** The full, unmapped API payload. */
  raw: Record<string, unknown>;
}

export interface RecallResult {
  summary: string | null;
  supportingMemories: Memory[];
  raw: Record<string, unknown>;
}

export interface MemClawOptions {
  tenantId: string;
  baseUrl?: string;
  agentId?: string;
  timeoutMs?: number;
  /** Inject a custom fetch (e.g. for tests). Defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
}

export interface WriteOptions {
  agentId?: string;
  memoryType?: string;
  fleetId?: string;
  metadata?: Record<string, unknown>;
  [extra: string]: unknown;
}

export interface SearchOptions {
  topK?: number;
  fleetIds?: string[];
  filterAgentId?: string;
  [extra: string]: unknown;
}

function toMemory(d: Record<string, any>): Memory {
  return {
    id: d.id ?? null,
    content: d.content ?? "",
    title: d.title ?? null,
    memoryType: d.memory_type ?? null,
    tenantId: d.tenant_id ?? null,
    agentId: d.agent_id ?? null,
    weight: d.weight ?? null,
    similarity: d.similarity ?? null,
    metadata: d.metadata ?? null,
    raw: d,
  };
}

export class MemClaw {
  readonly tenantId: string;
  readonly agentId?: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly headers: Record<string, string>;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(apiKey: string, options: MemClawOptions) {
    if (!apiKey) throw new Error("apiKey is required");
    if (!options || !options.tenantId) throw new Error("tenantId is required");
    this.tenantId = options.tenantId;
    this.agentId = options.agentId;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeoutMs = options.timeoutMs ?? 30000;
    this.headers = { "X-API-Key": apiKey, "Content-Type": "application/json" };
    const f = options.fetch ?? globalThis.fetch;
    if (!f) throw new Error("global fetch is unavailable; pass options.fetch or use Node 18+");
    this.fetchImpl = f;
  }

  /** Persist a memory. POST /api/v1/memories */
  async write(content: string, options: WriteOptions = {}): Promise<Memory> {
    const { agentId, memoryType, fleetId, metadata, ...extra } = options;
    const body: Record<string, unknown> = { tenant_id: this.tenantId, content };
    const resolvedAgent = agentId ?? this.agentId;
    if (resolvedAgent) body.agent_id = resolvedAgent;
    if (memoryType) body.memory_type = memoryType;
    if (fleetId) body.fleet_id = fleetId;
    if (metadata !== undefined) body.metadata = metadata;
    Object.assign(body, extra);
    return toMemory(await this.request("POST", "/api/v1/memories", body));
  }

  /** Hybrid vector + keyword search. POST /api/v1/search */
  async search(query: string, options: SearchOptions = {}): Promise<Memory[]> {
    const { topK = 5, fleetIds, filterAgentId, ...extra } = options;
    const body: Record<string, unknown> = { tenant_id: this.tenantId, query, top_k: topK };
    if (fleetIds) body.fleet_ids = fleetIds;
    if (filterAgentId) body.filter_agent_id = filterAgentId;
    Object.assign(body, extra);
    const data = await this.request("POST", "/api/v1/search", body);
    const items: unknown = data?.items;
    return Array.isArray(items) ? items.map((m) => toMemory(m as Record<string, any>)) : [];
  }

  /** Search + LLM-synthesized context brief. POST /api/v1/recall */
  async recall(query: string, options: { topK?: number } = {}): Promise<RecallResult> {
    const body = { tenant_id: this.tenantId, query, top_k: options.topK ?? 5 };
    const data = await this.request("POST", "/api/v1/recall", body);
    const supporting: unknown = data?.supporting_memories;
    return {
      summary: data?.summary ?? null,
      supportingMemories: Array.isArray(supporting)
        ? supporting.map((m) => toMemory(m as Record<string, any>))
        : [],
      raw: data,
    };
  }

  /** Liveness probe. GET /api/v1/health */
  async health(): Promise<Record<string, unknown>> {
    return this.request("GET", "/api/v1/health");
  }

  private async request(method: string, path: string, body?: unknown): Promise<any> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      res = await this.fetchImpl(this.baseUrl + path, {
        method,
        headers: this.headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    await raiseForStatus(res);
    return res.json();
  }
}

async function raiseForStatus(res: Response): Promise<void> {
  if (res.ok) return;
  let payload: any = {};
  try {
    payload = await res.json();
  } catch {
    payload = {};
  }
  let message = "";
  let details: unknown;
  if (payload && typeof payload === "object") {
    const err = payload.error;
    if (err && typeof err === "object") {
      message = err.message ?? "";
      details = err.details;
    }
    message = message || payload.detail || payload.message || "";
  }
  if (res.status === 401 || res.status === 403) {
    throw new AuthError(res.status, message || "authentication failed", details);
  }
  if (res.status === 404) {
    throw new NotFoundError(res.status, message || "not found", details);
  }
  throw new MemClawApiError(res.status, message || "request failed", details);
}
