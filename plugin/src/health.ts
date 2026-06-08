/**
 * MemClaw backend-reachability tracker.
 *
 * Process-wide state that remembers whether the MemClaw server (configured via
 * MEMCLAW_API_URL) is currently reachable from this plugin. Fed by:
 *   - `markReachable()` — called after any successful `apiCall` via the
 *     transport helpers, or by the heartbeat's periodic health probe.
 *   - `markUnreachable(reason)` — called when an `apiCall` throws a
 *     network-class error, or when the heartbeat probe returns an unhealthy
 *     result.
 *
 * Read by:
 *   - `registerMemoryRuntime.getMemorySearchManager` — when `state === "unreachable"`,
 *     returns `{manager: null, error}` to OpenClaw, which surfaces a typed
 *     "memory unavailable" result to the model instead of silently dropping
 *     the request.
 *   - `manager.probeEmbeddingAvailability` / `probeVectorAvailability` — real
 *     probes now, using cached state.
 *   - `manager.status()` — populates `fallback: {from, reason}` on unreachable.
 *   - Heartbeat `setup_status.backend_reachable` — surfaced to the Fleet UI.
 *
 * Rationale: before this module, the registerMemoryRuntime callbacks
 * `catch { return []; }` swallowed every error, so a pairing failure or an
 * unreachable backend looked like "no results" to the model. That coating
 * let the install-plugin bug live for weeks undetected.
 */

export type ReachabilityState = "reachable" | "unreachable" | "unknown";

export interface Reachability {
  state: ReachabilityState;
  lastCheckMs: number;
  reason?: string;
}

let _state: Reachability = { state: "unknown", lastCheckMs: 0 };

export function getReachability(): Reachability {
  return { ..._state };
}

export function markReachable(): void {
  _state = { state: "reachable", lastCheckMs: Date.now() };
}

export function markUnreachable(reason: string): void {
  _state = { state: "unreachable", lastCheckMs: Date.now(), reason };
}

/** Reset to unknown. Test-only hook; not exported via index.ts. */
export function _resetReachabilityForTests(): void {
  _state = { state: "unknown", lastCheckMs: 0 };
}

/** Heuristic: classify an error as network-class vs. server-logic-class.
 *  Network-class (connect refused, DNS, TCP reset, timeout, fetch failed,
 *  5xx) updates the reachability tracker. HTTP 4xx errors come from a
 *  reachable server saying "no" to a specific request and must NOT mark
 *  the backend unreachable.
 *
 *  AbortError is explicitly excluded — `AbortController.abort()` is called
 *  by timeouts and OpenClaw lifecycle management (e.g. request cancellation
 *  when a turn is interrupted), not by a genuine backend outage. Treating
 *  those as unreachability flips the tracker spuriously and suppresses the
 *  real backend from the runtime contract.
 */
export function isNetworkClassError(err: unknown): boolean {
  if (!err) return false;
  // DOMException / Node fetch aborts surface with name === "AbortError".
  // Guard by name before falling through to the message heuristic.
  if ((err as { name?: unknown })?.name === "AbortError") return false;
  const msg = String((err as { message?: unknown })?.message ?? err).toLowerCase();
  // Explicit HTTP-status classifications from transport.ts surface as
  // "http <status>: ..." — anything <500 is NOT network-class.
  const httpMatch = msg.match(/\bhttp\s+(\d{3})\b/);
  if (httpMatch) {
    const code = Number(httpMatch[1]);
    return code >= 500 && code < 600;
  }
  return (
    msg.includes("fetch failed") ||
    msg.includes("econnrefused") ||
    msg.includes("enotfound") ||
    msg.includes("eai_again") ||
    msg.includes("econnreset") ||
    msg.includes("etimedout") ||
    msg.includes("timeout") ||
    msg.includes("network") ||
    msg.includes("socket hang up")
  );
}

/**
 * Wrap an async operation so it updates the reachability tracker based on
 * outcome. Success → markReachable. Network-class failure → markUnreachable
 * with the error message. Non-network-class failures leave the tracker
 * untouched (a 404 on a memory id is not a backend outage).
 *
 * The original error (or result) is always re-thrown / returned unchanged —
 * this is purely a side-effect tracker.
 */
export async function trackReachability<T>(op: () => Promise<T>): Promise<T> {
  try {
    const r = await op();
    markReachable();
    return r;
  } catch (e) {
    if (isNetworkClassError(e)) {
      markUnreachable(String((e as { message?: unknown })?.message ?? e));
    }
    throw e;
  }
}
