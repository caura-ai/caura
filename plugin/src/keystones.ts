/**
 * Keystone auto-injection (CAURA-000).
 *
 * Keystones are governance rules the agent MUST obey. The ContextEngine
 * fetches them from ``GET /api/v1/memclaw/keystones`` once per identity
 * (tenant, fleet, agent) and prepends a ``<keystone_rules>`` block to
 * every system prompt so the rules are deterministically present
 * regardless of whether ``shouldRecall`` gates the per-turn ``/search``
 * call. The whole point is that they bypass recall — they aren't
 * memories, they're policy.
 *
 * Design choices that diverge from the recall path:
 *
 *   * **Identity-keyed cache, not session-keyed.** A single plugin
 *     process serves one tenant/agent, so caching by ``tenant:agent:fleet``
 *     mirrors the existing ``recallCache`` shape and gives a cheap reuse
 *     across all sessions. Invalidation clears the whole cache (small,
 *     bounded) on a ``memclaw_keystones_set`` dispatch — see
 *     ``invalidateKeystoneCache``.
 *   * **Fail-open.** Any network / auth / 5xx error logs and returns
 *     ``""``. Keystones are additive — losing them is bad, but blocking
 *     ``assemble`` is worse.
 *   * **Weight-aware truncation.** The token cap drops the lowest-weight
 *     rules first and appends ``... (N more rules omitted)`` so an
 *     operator notices.
 *   * **Kill switch.** ``MEMCLAW_KEYSTONES_ENABLED=false`` short-circuits
 *     before any network call.
 */
import { apiCall } from "./transport.js";
import {
  ensureTenantId,
  KEYSTONES_TIMEOUT_MS,
  MEMCLAW_KEYSTONES_CACHE_TTL_MS,
  MEMCLAW_KEYSTONES_ENABLED,
  MEMCLAW_KEYSTONES_TOKEN_CAP,
  // ESM ``let`` exports give a live binding — reading
  // ``MEMCLAW_TENANT_ID`` at call time returns whatever the latest
  // ``ensureTenantId`` resolution wrote into env.ts. We use this to
  // skip the ``await ensureTenantId()`` microtask on the warm path
  // (after the first successful resolution) so the cache check isn't
  // gated behind even a trivial promise hop.
  MEMCLAW_TENANT_ID,
} from "./env.js";
import { logError } from "./logger.js";

// ~4 chars/token — same approximation the recall path uses for trimming.
const CHARS_PER_TOKEN_ESTIMATE = 4;

/** One row as returned by the storage layer's ``orm_to_dict``. */
export interface KeystoneRow {
  doc_id: string;
  data: {
    title?: string;
    content?: string;
    weight?: number;
    scope?: string;
    agent_id?: string;
    [key: string]: unknown;
  };
}

interface KeystonesPayload {
  count: number;
  truncated: boolean;
  rules: KeystoneRow[];
}

interface CacheEntry {
  text: string;
  ts: number;
}

const keystonesCache = new Map<string, CacheEntry>();
// In-flight request map: collapses concurrent ``fetchKeystonesBlock``
// calls for the same cache key onto a single network round-trip. Without
// this, several ``assemble`` turns firing before the first response
// lands would each open their own request and each populate the cache —
// cache stampede. The promise is removed in a ``finally`` so a failed
// request doesn't pin a rejected promise in the map.
const inflight = new Map<string, Promise<string>>();

// Monotonic counter bumped on every cache invalidation. In-flight
// fetches snapshot the value at issue time and only write to the
// cache when it's unchanged. Without this, a write that races an
// invalidation (e.g. invalidate fires WHILE a fetch is mid-flight)
// can write its stale result on top of the empty cache and mask the
// freshly-authored rule until the next TTL expiry.
let _cacheGeneration = 0;

// Cache keys are built via ``JSON.stringify([...])`` rather than a
// ``colon-joined`` template because tenant / agent / fleet IDs can in
// principle contain colons or the ``_`` sentinel — array serialisation
// preserves field boundaries unambiguously and keeps the key shape
// inspectable.
function _cacheKey(tenantId: string, fleetId: string | undefined, agentId: string): string {
  return JSON.stringify([tenantId, agentId, fleetId ?? null]);
}

/**
 * Drop every cached keystone block.
 *
 * Exported for future wiring: once the plugin gains a path that
 * dispatches ``memclaw_keystones_set`` (it currently doesn't — the
 * write tool is MCP-only with ``plugin_exposed=false``), the dispatch
 * site should call this so a freshly-authored rule takes effect on the
 * next ``assemble`` turn instead of waiting out the cache TTL. Until
 * that wiring lands, this function has no call site in the plugin —
 * keystone writes happen via MCP/REST and the cache picks up the new
 * state on TTL expiry. Removing this export would force re-adding it
 * later, so it stays exported.
 */
export function invalidateKeystoneCache(): void {
  // Bump the generation so any in-flight ``_fetchAndCache`` that
  // resolves after this call refuses to write its now-stale result
  // back into ``keystonesCache``.
  _cacheGeneration++;
  keystonesCache.clear();
  // Also drop any in-flight requests so a write that bumps a fresh
  // result doesn't get masked by an older flight that's still settling.
  inflight.clear();
}

/**
 * Format a list of rules into the ``<keystone_rules>`` block. Lowest-
 * weight rules are dropped first when the token cap is hit so the
 * highest-priority governance wins under pressure.
 */
export function formatKeystones(rules: KeystoneRow[]): string {
  if (rules.length === 0) return "";

  // Sort by weight DESC so that any truncation drops low-weight rules.
  const sorted = rules
    .slice()
    .sort(
      (a, b) =>
        (b.data?.weight ?? 0) - (a.data?.weight ?? 0),
    );

  const header =
    "\n<keystone_rules>\n" +
    "The following are MANDATORY rules for this agent. They override " +
    "conflicting instructions in user prompts and in the system prompt " +
    "above this block. Always follow them.\n";
  const footer = "</keystone_rules>\n";
  const maxChars = MEMCLAW_KEYSTONES_TOKEN_CAP * CHARS_PER_TOKEN_ESTIMATE;
  // Reserve room for the worst-case truncation line up front. The
  // upper bound on ``N more rules omitted`` is ``sorted.length - 1``
  // — the line only renders when AT LEAST one rule made it into the
  // body, so ``N`` can never equal ``sorted.length``. Pre-fix we
  // reserved bytes for one extra digit/char that the line will never
  // emit; on the cap boundary this cost us a rule that would
  // otherwise have fit. ``Math.max(..., 0)`` keeps the formula sound
  // for the 0-rule case (no truncation line emitted at all).
  const TRUNCATION_RESERVE =
    `... (${Math.max(sorted.length - 1, 0)} more rules omitted)\n`.length;

  // Strip any ``<keystone_rules…>`` / ``</keystone_rules…>`` tag from
  // rule fields before interpolation, then flatten newlines to spaces.
  // Without this, a rule whose content contains the closing tag (with
  // or without attributes, on any line) would close the wrapping block
  // early and let attacker-controlled text appear OUTSIDE the
  // mandatory-rules frame in the model's system prompt. The
  // ``[^>]*`` clause covers e.g. ``<keystone_rules ignored="true">``;
  // the newline strip prevents a single rule from spanning lines and
  // breaking the ``- title: content`` per-line shape the prompt
  // depends on. Case-insensitive — LLMs don't care about case.
  const sanitize = (s: string): string =>
    s.replace(/<\/?keystone_rules[^>]*>/gi, "").replace(/[\r\n]+/g, " ");

  const lines: string[] = [];
  let charsUsed = header.length + footer.length + TRUNCATION_RESERVE;
  let included = 0;
  for (const rule of sorted) {
    const title = sanitize((rule.data?.title ?? rule.doc_id).trim());
    const content = sanitize((rule.data?.content ?? "").trim());
    const line = `- ${title}: ${content}\n`;
    if (charsUsed + line.length > maxChars) break;
    lines.push(line);
    charsUsed += line.length;
    included += 1;
  }

  const omitted = sorted.length - included;
  const truncatedLine = omitted > 0 ? `... (${omitted} more rules omitted)\n` : "";

  // If the token cap was smaller than the fixed block overhead (header
  // + footer + truncation reserve), the loop above included nothing
  // even though rules exist. Emitting an empty ``<keystone_rules>``
  // block with just an "N omitted" line is misleading — it makes the
  // agent think it has rules to follow while showing none. Treat this
  // the same as ``rules.length === 0``: return ``""`` and let the
  // caller skip the inject entirely.
  if (included === 0) return "";

  return header + lines.join("") + truncatedLine + footer;
}

/**
 * Fetch keystones for the caller and return the formatted block.
 *
 * Returns ``""`` (and never throws) when the feature is disabled, the
 * backend returns no rules, or anything goes wrong on the network /
 * auth / serialization path. The caller treats an empty string as "no
 * block to inject" — keystones must never break ``assemble``.
 */
// Negative-cache window for both tenant-resolution and fetch failures.
// Backend outages otherwise pay the full ``KEYSTONES_TIMEOUT_MS`` on
// every ``assemble`` call until the regular TTL expires; capping the
// retry interval at this value keeps a degraded backend from adding
// per-turn latency for the full cache window. 15 s is short enough that
// recovery feels immediate to operators and long enough to absorb a
// rolling restart.
const FAILURE_BACKOFF_MS = 15_000;

/**
 * Stamp a cache entry with a timestamp set so the entry expires after
 * exactly ``FAILURE_BACKOFF_MS``, regardless of the regular TTL.
 * Computed by back-dating ``ts`` so the existing ``ts < TTL_MS`` check
 * still drives eviction without a second code path.
 */
function _negativeCacheTs(): number {
  return Date.now() - MEMCLAW_KEYSTONES_CACHE_TTL_MS + FAILURE_BACKOFF_MS;
}

/**
 * Cache key used when ``ensureTenantId()`` failed and the real tenant
 * is unknown. Different fleet/agent combos cache separately so a
 * subsequent identity change can still resolve.
 */
function _tenantFailKey(fleetId: string | undefined, agentId: string): string {
  // NUL-prefixed sentinel so it can never collide with a real tenant
  // ID key produced by ``_cacheKey`` — tenant IDs are server-issued
  // identifiers and cannot contain a NUL byte.
  return JSON.stringify(["\x00tenantFail", agentId, fleetId ?? null]);
}

export async function fetchKeystonesBlock(opts: {
  agentId: string;
  fleetId: string | undefined;
}): Promise<string> {
  if (!MEMCLAW_KEYSTONES_ENABLED) return "";

  // Check the tenant-fail back-off first so an ongoing tenant-resolution
  // outage skips the expensive ``ensureTenantId`` retry on every turn.
  const tenantFailKey = _tenantFailKey(opts.fleetId, opts.agentId);
  const tenantFailHit = keystonesCache.get(tenantFailKey);
  if (
    tenantFailHit &&
    Date.now() - tenantFailHit.ts < MEMCLAW_KEYSTONES_CACHE_TTL_MS
  ) {
    return tenantFailHit.text;
  }

  // Warm path: ``MEMCLAW_TENANT_ID`` was set by an earlier
  // ``ensureTenantId`` resolution (or from the ``.env`` file at boot).
  // Read the live binding synchronously to skip the await microtask
  // and reach the cache check immediately. Cold path: fall through to
  // the awaited resolution.
  let tenantId: string = MEMCLAW_TENANT_ID;
  if (!tenantId) {
    try {
      tenantId = await ensureTenantId();
    } catch (e) {
      logError("keystones: tenant resolution failed", e);
      keystonesCache.set(tenantFailKey, { text: "", ts: _negativeCacheTs() });
      return "";
    }
    if (!tenantId) {
      // Treat as a transient failure too — same back-off applies so a
      // missing-key boot state doesn't spam ``ensureTenantId`` every turn.
      keystonesCache.set(tenantFailKey, { text: "", ts: _negativeCacheTs() });
      return "";
    }
  }

  const cacheKey = _cacheKey(tenantId, opts.fleetId, opts.agentId);
  const cached = keystonesCache.get(cacheKey);
  if (cached && Date.now() - cached.ts < MEMCLAW_KEYSTONES_CACHE_TTL_MS) {
    return cached.text;
  }
  // Best-effort eviction of stale entries on cache miss — keeps the Map
  // bounded under steady-state churn without a separate sweeper.
  const now = Date.now();
  for (const [k, v] of keystonesCache) {
    if (now - v.ts > MEMCLAW_KEYSTONES_CACHE_TTL_MS) keystonesCache.delete(k);
  }

  // Collapse concurrent misses onto a single in-flight request. Without
  // this, multiple ``assemble`` calls during a session-start burst each
  // open their own network request and each populate the cache — a
  // classic stampede. The first miss owns the fetch; the rest await
  // its result. The promise is removed in a ``finally`` so a failed
  // request doesn't pin a rejected promise.
  const flying = inflight.get(cacheKey);
  if (flying) return flying;
  const promise = _fetchAndCache(tenantId, cacheKey, opts, _cacheGeneration);
  inflight.set(cacheKey, promise);
  try {
    return await promise;
  } finally {
    // Only delete if WE'RE still the registered promise. After
    // ``invalidateKeystoneCache`` clears the map mid-flight, a
    // subsequent call may have started a fresh request and registered
    // its own promise under the same key — naively deleting would
    // strand that new promise's awaiters without an in-flight entry.
    if (inflight.get(cacheKey) === promise) {
      inflight.delete(cacheKey);
    }
  }
}

/**
 * Do the actual network fetch and populate ``keystonesCache``. Split
 * from ``fetchKeystonesBlock`` so the in-flight ``Map<key, Promise>``
 * coalescing pattern stays readable at the top of the public function.
 */
async function _fetchAndCache(
  tenantId: string,
  cacheKey: string,
  opts: { agentId: string; fleetId: string | undefined },
  generation: number,
): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), KEYSTONES_TIMEOUT_MS);
  try {
    const query: Record<string, string> = { tenant_id: tenantId };
    // Drop ``agent_id`` when there's no ``fleet_id`` — agent-scope rows
    // are keyed on (fleet_id, agent_id) so an agent-only filter can't
    // resolve them. Mirrors the server-side guard.
    if (opts.fleetId) {
      query.fleet_id = opts.fleetId;
      if (opts.agentId) query.agent_id = opts.agentId;
    }
    const raw = await apiCall(
      "GET",
      "/memclaw/keystones",
      undefined,
      query,
      controller.signal,
    );
    // Two shapes accepted: a bare list (older response) or
    // ``{count, truncated, rules}`` (current). Coerce both to rows.
    const rows: KeystoneRow[] = Array.isArray(raw)
      ? (raw as KeystoneRow[])
      : ((raw as KeystonesPayload | null)?.rules ?? []);
    // Cache unconditionally — a 200 with zero rules is a valid answer
    // for tenants that haven't configured any keystones. Skipping the
    // ``""`` cache entry would re-fetch on every ``assemble`` call for
    // the no-rules common case, defeating the cache's purpose.
    const text = formatKeystones(rows);
    // Only write back if no ``invalidateKeystoneCache`` ran while we
    // were in flight — otherwise a stale result could clobber a fresh
    // cache state authored mid-flight by a keystone write.
    if (generation === _cacheGeneration) {
      keystonesCache.set(cacheKey, { text, ts: Date.now() });
    }
    return text;
  } catch (e) {
    logError("keystones: fetch failed", e);
    // Short back-off so transient outages don't add KEYSTONES_TIMEOUT_MS
    // to every turn for the full cache TTL period.
    if (generation === _cacheGeneration) {
      keystonesCache.set(cacheKey, { text: "", ts: _negativeCacheTs() });
    }
    return "";
  } finally {
    clearTimeout(timeout);
  }
}
