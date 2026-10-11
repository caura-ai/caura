/**
 * Official TypeScript/JavaScript client for Caura — governed shared memory
 * for AI agent fleets. A thin wrapper over the Caura REST API.
 *
 * Point it at a managed (`https://caura.ai`) or self-hosted
 * (`http://localhost:8000`) deployment.
 */

import { VERSION } from "./version.js";

export { VERSION };

export const DEFAULT_BASE_URL = "https://caura.ai";

/**
 * Sent on every request so a server can tell SDK families apart. Names the
 * package, its version and, under Node, the Node major; nothing else. In a
 * browser `fetch` ignores a caller-supplied User-Agent, which is fine.
 */
export const USER_AGENT = `caura-client-node/${VERSION}${runtimeTag()}`;

function runtimeTag(): string {
  const node = (globalThis as { process?: { versions?: { node?: string } } }).process?.versions?.node;
  return node ? ` (node/${node.split(".")[0]})` : "";
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[(.*)\]$/, "$1");
  return (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "::1" ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)
  );
}

function envAllowsInsecureHttp(): boolean {
  const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;
  return ["true", "1"].includes(env?.CAURA_ALLOW_INSECURE_HTTP ?? "");
}

/**
 * Refuse to send the API key in cleartext to another machine (L-66): https, or
 * plain http to a loopback host, or an explicit opt-in. The same rule as the
 * OpenClaw plugin's `keyTransportPolicy`.
 */
function assertKeyTransportAllowed(baseUrl: string, allowInsecureHttp: boolean | undefined): void {
  const expected = "baseUrl must start with https:// (or http:// for a loopback host)";
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    throw new Error(`${expected}; got an invalid URL`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`${expected}; got ${url.protocol}`);
  }
  if (url.protocol === "https:" || isLoopbackHost(url.hostname)) return;
  if (allowInsecureHttp ?? envAllowsInsecureHttp()) return;
  throw new Error(
    `Refusing to send the API key to ${url.host}: baseUrl uses plain HTTP to a non-loopback host, ` +
      `so the key would cross the network in cleartext. Use https://, or pass allowInsecureHttp: true ` +
      `(or set CAURA_ALLOW_INSECURE_HTTP=true) to accept the risk, e.g. on a trusted private network.`,
  );
}

export class CauraError extends Error {}

/** Raised on network failures or timeouts, retaining the original error as cause. */
export class TransportError extends CauraError {
  constructor(cause: unknown) {
    super(`Request failed: ${cause instanceof Error ? cause.message : String(cause)}`, { cause });
    this.name = "TransportError";
  }
}

export class CauraApiError extends CauraError {
  readonly statusCode: number;
  readonly details: unknown;
  constructor(statusCode: number, message: string, details?: unknown) {
    super(`[${statusCode}] ${message}`);
    this.name = "CauraApiError";
    this.statusCode = statusCode;
    this.details = details;
  }
}

/** Raised on 401/403 — bad or insufficiently-scoped credential. */
export class AuthError extends CauraApiError {}

/** Raised on 404. */
export class NotFoundError extends CauraApiError {}

/** Raised on 429, with the optional retry delay in seconds. */
export class RateLimitError extends CauraApiError {
  readonly retryAfter: number | null;
  constructor(statusCode: number, message: string, details?: unknown, retryAfter: number | null = null) {
    super(statusCode, message, details);
    this.retryAfter = retryAfter;
  }
}

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

/**
 * The envelope `/search` returns around its results (L-94). `search()` returns
 * the ranked memories as an array that also carries these fields.
 */
export interface SearchEnvelope {
  /** Whether this search reinforced the memories it returned; null when the server did not say. */
  recallTracked: boolean | null;
  /** The retrieval trace a `diagnostic: true` search returns, else null. */
  diagnostic: Record<string, unknown> | null;
  /** Coded caveats about the result set, such as a parameter the server ignored, else null. */
  warnings: Array<Record<string, unknown>> | null;
  /** The whole response body. */
  raw: Record<string, unknown>;
}

/** The ranked memories, as an array, with the envelope on it. */
export type SearchResult = Memory[] & SearchEnvelope;

export interface RecallResult {
  summary: string | null;
  supportingMemories: Memory[];
  raw: Record<string, unknown>;
}

export interface CauraOptions {
  tenantId: string;
  baseUrl?: string;
  agentId?: string;
  timeoutMs?: number;
  /** Inject a custom fetch (e.g. for tests). Defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
  /**
   * Send the API key over plain http to a non-loopback host. Off by default: the
   * key would cross the network in cleartext. Unset defers to
   * `CAURA_ALLOW_INSECURE_HTTP` (`true` or `1`); an explicit `false` beats it.
   */
  allowInsecureHttp?: boolean;
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
  /**
   * Read as this agent without filtering to its own memories, so a
   * tenant-scoped key can read the agent's `scope_agent` memories. The server
   * then registers the agent if new and holds the read to its fleet and trust
   * level. Sent only when set; an agent-scoped key may only name its own agent.
   */
  callerAgentId?: string;
  [extra: string]: unknown;
}

export interface RecallOptions {
  topK?: number;
  /** As for `search`. */
  callerAgentId?: string;
  [extra: string]: unknown;
}

export interface GetDocumentOptions {
  collection: string;
  tenantId?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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

export class Caura {
  readonly tenantId: string;
  readonly agentId?: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly headers: Record<string, string>;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(apiKey: string, options: CauraOptions) {
    if (!apiKey) throw new Error("apiKey is required");
    if (!options || !options.tenantId) throw new Error("tenantId is required");
    this.tenantId = options.tenantId;
    this.agentId = options.agentId;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    assertKeyTransportAllowed(this.baseUrl, options.allowInsecureHttp);
    this.timeoutMs = options.timeoutMs ?? 30000;
    this.headers = {
      "X-API-Key": apiKey,
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
    };
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

  /**
   * Hybrid vector + keyword search. POST /api/v1/search
   *
   * Resolves to the ranked memories as an array that also carries the response
   * envelope (`recallTracked`, `diagnostic`, `warnings`, `raw`).
   */
  async search(query: string, options: SearchOptions = {}): Promise<SearchResult> {
    const { topK = 5, fleetIds, filterAgentId, callerAgentId, ...extra } = options;
    const body: Record<string, unknown> = { tenant_id: this.tenantId, query, top_k: topK };
    if (fleetIds) body.fleet_ids = fleetIds;
    if (filterAgentId) body.filter_agent_id = filterAgentId;
    if (callerAgentId) body.caller_agent_id = callerAgentId;
    Object.assign(body, extra);
    const data = await this.request("POST", "/api/v1/search", body);
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new CauraApiError(200, "search response must be a JSON object");
    }
    if (!("items" in data)) {
      throw new CauraApiError(200, 'search response missing "items" list');
    }
    const payload = data as Record<string, unknown>;
    const items = payload.items;
    if (!Array.isArray(items)) {
      throw new CauraApiError(200, 'search response "items" must be a list');
    }
    const { recall_tracked: recallTracked, diagnostic, warnings } = payload;
    const envelope: SearchEnvelope = {
      recallTracked: typeof recallTracked === "boolean" ? recallTracked : null,
      diagnostic: isRecord(diagnostic) ? diagnostic : null,
      warnings: Array.isArray(warnings) ? warnings : null,
      raw: payload,
    };
    const memories = items.map((m) => toMemory(m as Record<string, any>));
    return Object.assign(memories, envelope);
  }

  /**
   * Search + LLM-synthesized context brief. POST /api/v1/recall
   *
   * Asks for the result list once (`items_alias: false`): the server would
   * otherwise repeat it under `items`, about half the response, and this client
   * reads `memories`. Pass `items_alias: true` to keep the copy in `raw`.
   */
  async recall(query: string, options: RecallOptions = {}): Promise<RecallResult> {
    const { topK = 5, callerAgentId, ...extra } = options;
    const body: Record<string, unknown> = {
      tenant_id: this.tenantId,
      query,
      top_k: topK,
      items_alias: false,
    };
    if (callerAgentId) body.caller_agent_id = callerAgentId;
    Object.assign(body, extra);
    const data = await this.request("POST", "/api/v1/recall", body);
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new CauraApiError(200, "recall response must be a JSON object");
    }
    // Wire key is `memories`; the server aliases the identical list under
    // `items` too, for consumers written against /search's shape.
    //
    // H-01: this read `data?.supporting_memories`, a key the server has never
    // emitted in any commit — it was invented in the Python SDK and mirrored
    // here, so every recall() returned [] while `summary` kept working, and the
    // test below mocked the invented shape so CI stayed green. The RESULT FIELD
    // keeps its name (`supportingMemories`) since that is published API; only
    // the wire key was wrong.
    const payload = data as Record<string, unknown>;
    const supporting: unknown = payload.memories ?? payload.items;
    return {
      summary: typeof payload.summary === "string" ? payload.summary : null,
      supportingMemories: Array.isArray(supporting)
        ? supporting.map((m) => toMemory(m as Record<string, any>))
        : [],
      raw: data,
    };
  }

  /** Fetch one structured document. GET /api/v1/documents/{docId} */
  async getDocument(docId: string, options: GetDocumentOptions): Promise<Record<string, unknown>> {
    const encoded = encodeURIComponent(docId);
    const tenant = options.tenantId || this.tenantId;
    const params = new URLSearchParams({
      tenant_id: tenant,
      collection: options.collection,
    });
    return this.request("GET", `/api/v1/documents/${encoded}?${params.toString()}`);
  }

  /** Liveness probe. GET /api/v1/health */
  async health(): Promise<Record<string, unknown>> {
    return this.request("GET", "/api/v1/health");
  }

  private async request(method: string, path: string, body?: unknown): Promise<any> {
    const serializedBody = body !== undefined ? JSON.stringify(body) : undefined;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      let res: Response;
      try {
        res = await this.fetchImpl(this.baseUrl + path, {
          method,
          headers: this.headers,
          body: serializedBody,
          signal: controller.signal,
          // fetch re-sends X-API-Key to a redirect target, even cross-origin
          // or from https to http, so a redirect is an error (L-66).
          redirect: "error",
        });
      } catch (cause) {
        throw new TransportError(cause);
      }
      await raiseForStatus(res);
      return await readResponseJson(res);
    } finally {
      clearTimeout(timer);
    }
  }
}

async function readResponseJson(res: Response): Promise<any> {
  try {
    return await res.json();
  } catch (cause) {
    if (cause instanceof SyntaxError) throw cause;
    throw new TransportError(cause);
  }
}

async function raiseForStatus(res: Response): Promise<void> {
  if (res.ok) return;
  let payload: any = {};
  try {
    payload = await readResponseJson(res);
  } catch (cause) {
    if (!(cause instanceof SyntaxError)) throw cause;
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
  if (res.status === 429) {
    const retryAfter = res.headers.get("retry-after");
    const parsed = retryAfter === null ? Number.NaN : Number(retryAfter);
    throw new RateLimitError(
      res.status,
      message || "rate limit exceeded",
      details,
      Number.isFinite(parsed) ? parsed : null,
    );
  }
  throw new CauraApiError(res.status, message || "request failed", details);
}

// Rename compatibility aliases (2026-08) — same classes/types, so
// instanceof and catch clauses agree across old and new spellings.
export const MemClaw = Caura; // legacy-name-ok: published class alias
export type MemClaw = Caura; // legacy-name-ok: published class alias
export const MemClawError = CauraError; // legacy-name-ok: published exception alias
export type MemClawError = CauraError; // legacy-name-ok: published exception alias
export const MemClawApiError = CauraApiError; // legacy-name-ok: published exception alias
export type MemClawApiError = CauraApiError; // legacy-name-ok: published exception alias
export type MemClawOptions = CauraOptions; // legacy-name-ok: published options-type alias
