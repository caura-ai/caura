/**
 * HTTP transport layer for Caura API communication.
 *
 * Security fixes:
 * - Default 15s timeout on all requests via AbortController
 * - Signal forwarding from callers
 * - API key refused over plain HTTP to a non-loopback host
 */

import {AsyncLocalStorage} from "node:async_hooks";
import {CAURA_API_PREFIX, CAURA_API_URL, apiFetch, getAuthMode} from "./env.js";
const runAgent = new AsyncLocalStorage<string>();
/** Trusted host tool-run boundary; payload filters never establish the principal. */
export function withAgentAuthority<T>(agent: string, action: () => Promise<T>): Promise<T> {return runAgent.run(agent, action);}

const DEFAULT_TIMEOUT_MS = 15_000;

// Only the skill delivery surface has this wire contract. Count decoded
// response bytes while streaming, including failures and dishonest lengths.
const SKILL_RESPONSE_MAX = 4 * 1024 * 1024;
async function skillResponseText(res: Response): Promise<string> {
  const declared = Number(res.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > SKILL_RESPONSE_MAX) {
    await res.body?.cancel();
    throw new Error("Skill response exceeds 4 MiB");
  }
  if (!res.body) return "";
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > SKILL_RESPONSE_MAX) throw new Error("Skill response exceeds 4 MiB");
      chunks.push(value);
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}
type CallArgs = Parameters<typeof call> extends [unknown, ...infer Args] ? Args : never;
export const apiCall = (...args: CallArgs) => call("agent", ...args);
export const nodeApiCall = (...args: CallArgs) => call("install", ...args);
async function call(
  authority: "agent" | "install",
  method: string,
  path: string,
  body?: Record<string, unknown>,
  query?: Record<string, string>,
  signal?: AbortSignal,
  agentId?: string,
  extraHeaders?: Record<string, string>,
  onResponseHeaders?: (headers: Headers) => void,
): Promise<unknown> {
  const start = Date.now();

  // Contract: callers pass RESOURCE paths ("/memories", "/evolve/report").
  // Reject prefixed paths early so accidental full-path regressions are loud
  // during tsc/tests rather than silently 404'ing at runtime.
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (normalized.startsWith(CAURA_API_PREFIX + "/") || normalized === CAURA_API_PREFIX) {
    throw new Error(
      `[caura] apiCall path must be a resource path (e.g. "/evolve/report"); ` +
      `got "${path}". CAURA_API_PREFIX ("${CAURA_API_PREFIX}") is applied automatically.`,
    );
  }
  const url = new URL(`${CAURA_API_PREFIX}${normalized}`, CAURA_API_URL);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      // Callers cast optional fields into this map, and set() would send them
      // as the text "undefined" or "null", a value the server accepts (L-58).
      if (v === undefined || v === null) continue;
      url.searchParams.set(k, v);
    }
  }

  const actor = agentId ?? runAgent.getStore();
  if (authority === "agent" && getAuthMode() === "manual" && !runAgent.getStore())
    throw new Error("runtime_auth_manual_tools_only");
  const headers = {...extraHeaders, ...(body ? {"Content-Type": "application/json"} : {})};

  // Use caller's signal if provided, otherwise create a default timeout
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  let effectiveSignal = signal;

  if (!effectiveSignal) {
    const controller = new AbortController();
    effectiveSignal = controller.signal;
    timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  }

  try {
    const requestInit: RequestInit = {
      method,
      // X-API-Key is not stripped on cross-origin redirects by fetch.
      // Require the configured API URL to be the final destination.
      redirect: "error",
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: effectiveSignal,
    };
    const res = await apiFetch(authority, method, normalized + url.search, requestInit, actor);

    const skillResponse = normalized === "/skills/installable" || normalized === "/skills/installations";
    let deliveryText = skillResponse ? await skillResponseText(res) : undefined;

    if (!res.ok) {
      const text = deliveryText ?? await res.text();
      // Authentication errors never echo server bodies. Other 4xx retain bounded, redacted detail.
      let safeText = res.status >= 500 ? "server error" : "request refused";
      if (res.status === 401 || res.status === 403) {
        if (text.includes("agent_key_revoked")) safeText = "agent_key_revoked";
      } else if (res.status >= 400 && res.status < 500) {
        const echoText = text.replace(/\\"/g, '"');
        const credentialEcho = /\bheaders?["']?\s*[:=]\s*[\[{]/i.test(echoText) ||
          /\b(?:authorization|proxy-authorization|cookie|x-api-key|x-gateway-secret|(?:api[_-]?)?key|token|credential)["']?\s*[:=]\s*["']?[^\s"',}\]]/i.test(echoText) ||
          /^\s*(?:authorization|proxy-authorization|cookie|x-api-key|x-gateway-secret)\s+(?:(?:Bearer|Basic)\s+)?\S+\s*$/i.test(echoText) ||
          /\[\s*["'](?:authorization|proxy-authorization|cookie|x-api-key|x-gateway-secret)["']\s*,\s*["'][^"']+/i.test(echoText);
        if (!credentialEcho) safeText = text.replace(/\b(?:mc[a-z]?_|caura_|sk[-_]|gh[pousr]_|github_pat_|xox[baprs]-)[\w.+~/-]+/gi, "[redacted]")
          .replace(/\\\//g, "/")
          .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>]*@[^\s"'<>]*/gi, "[redacted-url]")
          .replace(/[\x00-\x1f\x7f]/g, " ").slice(0, 200);
      }
      throw new Error(`Caura API ${res.status}: ${safeText}`);
    }

    // Expose metadata from the final successful response without changing
    // the JSON payload contract for existing callers (including arrays).
    onResponseHeaders?.(res.headers);

    // 204 No Content (e.g. DELETE)
    if (res.status === 204) {
      return { ok: true, _latency_ms: Date.now() - start };
    }

    const data = deliveryText === undefined ? await res.json() : JSON.parse(deliveryText);
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
 * ``SearchResponse(items=...)``), while the MCP ``caura_recall`` path
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
