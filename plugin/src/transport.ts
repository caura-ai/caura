/**
 * HTTP transport layer for MemClaw API communication.
 *
 * Security fixes:
 * - Default 15s timeout on all requests via AbortController
 * - Signal forwarding from callers
 * - HTTPS enforced by default
 */

import { MEMCLAW_API_PREFIX, MEMCLAW_API_URL, MEMCLAW_API_KEY } from "./env.js";
import { resolveAgentKey, evictAgentKey } from "./agent-auth.js";

const DEFAULT_TIMEOUT_MS = 15_000;

export async function apiCall(
  method: string,
  path: string,
  body?: Record<string, unknown>,
  query?: Record<string, string>,
  signal?: AbortSignal,
  agentId?: string,
): Promise<unknown> {
  const start = Date.now();

  // Contract: callers pass RESOURCE paths ("/memories", "/evolve/report").
  // Reject prefixed paths early so accidental full-path regressions are loud
  // during tsc/tests rather than silently 404'ing at runtime.
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (normalized.startsWith(MEMCLAW_API_PREFIX + "/") || normalized === MEMCLAW_API_PREFIX) {
    throw new Error(
      `[memclaw] apiCall path must be a resource path (e.g. "/evolve/report"); ` +
      `got "${path}". MEMCLAW_API_PREFIX ("${MEMCLAW_API_PREFIX}") is applied automatically.`,
    );
  }
  const url = new URL(`${MEMCLAW_API_PREFIX}${normalized}`, MEMCLAW_API_URL);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      url.searchParams.set(k, v);
    }
  }

  // Resolve agent-scoped credential, or fall back to the tenant-scoped key
  const effectiveAgentId = agentId || (body?.agent_id as string) || (query?.agent_id as string);
  let effectiveKey = MEMCLAW_API_KEY;
  if (effectiveAgentId) {
    const agentKey = await resolveAgentKey(effectiveAgentId);
    if (agentKey) effectiveKey = agentKey;
  }

  const headers: Record<string, string> = {};
  if (body) headers["Content-Type"] = "application/json";
  if (effectiveKey) headers["X-API-Key"] = effectiveKey;

  // Use caller's signal if provided, otherwise create a default timeout
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  let effectiveSignal = signal;

  if (!effectiveSignal) {
    const controller = new AbortController();
    effectiveSignal = controller.signal;
    timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  }

  try {
    const res = await fetch(url.toString(), {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: effectiveSignal,
    });

    if (!res.ok) {
      // 401 with agent key → evict and retry once with tenant key
      if (res.status === 401 && effectiveKey !== MEMCLAW_API_KEY && effectiveAgentId) {
        evictAgentKey(effectiveAgentId);
        console.warn(`[memclaw] Agent key rejected for '${effectiveAgentId}', retrying with tenant key`);
        return apiCall(method, path, body, query, signal);  // retry without agentId → uses tenant key
      }
      const text = await res.text();
      // Truncate server error body to avoid leaking internal details
      const safeText = text.length > 200 ? text.slice(0, 200) + "..." : text;
      throw new Error(`MemClaw API ${res.status}: ${safeText}`);
    }

    // 204 No Content (e.g. DELETE)
    if (res.status === 204) {
      return { ok: true, _latency_ms: Date.now() - start };
    }

    const data = await res.json();
    const latency_ms = Date.now() - start;
    const serverTime = res.headers.get("X-Response-Time");
    if (typeof data === "object" && data !== null && !Array.isArray(data)) {
      (data as Record<string, unknown>)._latency_ms = latency_ms;
      if (serverTime)
        (data as Record<string, unknown>)._server_ms = parseInt(serverTime, 10);
    }
    return data;
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

/**
 * Extract the memory array from a ``POST /search`` response.
 *
 * The REST search endpoint returns ``{ items: [...] }`` (core-api
 * ``SearchResponse(items=...)``), while the MCP ``memclaw_recall`` path
 * returns ``{ results: [...] }`` and some legacy/test shims return a bare
 * array. Reading only ``.results`` (as we did pre-fix) silently yielded
 * ``[]`` against the REST backend — breaking context-engine auto-recall
 * and the bootstrap smoke test. Prefer ``items`` (the live REST contract),
 * fall back to ``results``, and accept a bare array. Never throws.
 */
export function parseSearchItems(sr: unknown): Record<string, unknown>[] {
  if (Array.isArray(sr)) return sr as Record<string, unknown>[];
  const o = sr as Record<string, unknown> | null | undefined;
  // Guard with Array.isArray: a malformed response like ``{ items: {} }``
  // or ``{ items: "..." }`` would otherwise slip through the cast and make
  // callers' ``.map`` / ``.length`` throw — breaking the "never throws"
  // contract above. Non-array → ``[]``.
  const arr = o?.items ?? o?.results;
  return Array.isArray(arr) ? (arr as Record<string, unknown>[]) : [];
}

export function textResult(text: string): {
  content: Array<{ type: string; text: string }>;
  details: Record<string, unknown>;
} {
  return {
    content: [{ type: "text", text }],
    details: {},
  };
}
