/**
 * Environment configuration and tenant resolution for Caura plugin.
 *
 * Security fixes:
 * - .env parse errors are logged (no silent swallow)
 * - HTTPS warning on insecure URL
 */

import { readFileSync, existsSync } from "fs";
import { getPluginEnvPath } from "./paths.js";
import { warnIfInsecureUrl } from "./validation.js";

/**
 * Keys a plugin ``.env`` file may inject into ``process.env``. Fully anchored
 * on purpose — without the bound a ``.env`` could hijack PATH, NODE_OPTIONS,
 * etc. This is the security boundary; keep it strict.
 *
 * Both prefixes are accepted so a ``CAURA_*`` line is not silently dropped;
 * the old prefix keeps working forever.
 */
export function isPluginEnvKey(key: string): boolean {
  return /^(?:CAURA|MEMCLAW)_[A-Z_]+$/.test(key);  // legacy-name-ok: rule 3 dual-read alias
}

/**
 * Whether a key belongs to the plugin's ``.env`` at all — deliberately looser
 * than ``isPluginEnvKey``: it answers "is this ours to carry", not "may this
 * reach process.env".
 *
 * Kept permissive because ``deploy.ts`` uses it to PRESERVE an operator's
 * existing keys across a redeploy. Tightening it here would quietly delete
 * anything the strict form rejects (``CAURA_FOO2``, lowercase) from a file we
 * do not own the contents of.
 */
export function hasPluginEnvPrefix(key: string): boolean {
  return /^(?:CAURA|MEMCLAW)_/.test(key);  // legacy-name-ok: rule 3 dual-read alias
}

// --- Load .env file from plugin directory ---
try {
  const envPath = getPluginEnvPath();
  if (existsSync(envPath)) {
    for (const line of readFileSync(envPath, "utf-8").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq < 1) continue;
      const key = trimmed.slice(0, eq).trim();
      let val = trimmed.slice(eq + 1).trim();
      // Handle quoted values
      if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      ) {
        val = val.slice(1, -1);
      }
      if (!isPluginEnvKey(key)) continue;
      process.env[key] = val;
    }
  }
} catch (e: unknown) {
  const msg = e instanceof Error ? e.message : String(e);
  console.warn("[caura] Failed to parse .env file:", msg);
}

/**
 * Read the first alias that carries a value. Aliases are listed new name
 * first, so ``CAURA_X`` wins when both are set.
 *
 * A blank alias never shadows a working one, but a lone ``CAURA_X=`` still
 * resolves to ``""`` rather than "unset" — the distinction only matters to
 * ``_readBoolEnv`` below, where ``""`` means off and unset means the default.
 */
export function readEnv(names: readonly string[]): string | undefined {
  let sawBlank = false;
  for (const name of names) {
    const value = process.env[name];
    if (value) return value;
    if (value !== undefined) sawBlank = true;
  }
  return sawBlank ? "" : undefined;
}

export const MEMCLAW_API_URL =
  readEnv(["CAURA_API_URL", "MEMCLAW_API_URL"]) || "http://localhost:8000";  // legacy-name-ok: rule 3 dual-read alias

/**
 * Prefix for all Caura REST routes. Single source of truth for API
 * versioning — bump to "/api/v2" here when the backend ships a new version.
 *
 * The transport layer auto-prepends this to relative paths. Raw fetch
 * sites use it via template literal.
 */
export const MEMCLAW_API_PREFIX = readEnv(["CAURA_API_PREFIX", "MEMCLAW_API_PREFIX"]) || "/api/v1";  // legacy-name-ok: rule 3 dual-read alias
export const MEMCLAW_API_KEY = readEnv(["CAURA_API_KEY", "MEMCLAW_API_KEY"]) || "";  // legacy-name-ok: rule 3 dual-read alias
export const MEMCLAW_FLEET_ID = readEnv(["CAURA_FLEET_ID", "MEMCLAW_FLEET_ID"]) || "";  // legacy-name-ok: rule 3 dual-read alias
export let MEMCLAW_TENANT_ID = readEnv(["CAURA_TENANT_ID", "MEMCLAW_TENANT_ID"]) || "";  // legacy-name-ok: rule 3 dual-read alias
export const MEMCLAW_NODE_NAME = readEnv(["CAURA_NODE_NAME", "MEMCLAW_NODE_NAME"]) || "";  // legacy-name-ok: rule 3 dual-read alias
export const MEMCLAW_AGENT_ID = readEnv(["CAURA_AGENT_ID", "MEMCLAW_AGENT_ID"]) || "";  // legacy-name-ok: rule 3 dual-read alias
// Default to true — auto-writing turn summaries is the core fix for the
// "100% dark matter" problem (memories written but never recalled).
// Users can opt out with MEMCLAW_AUTO_WRITE_TURNS=false.
export const MEMCLAW_AUTO_WRITE_TURNS =
  readEnv(["CAURA_AUTO_WRITE_TURNS", "MEMCLAW_AUTO_WRITE_TURNS"]) !== "false";  // legacy-name-ok: rule 3 dual-read alias

// HMAC signature enforcement on incoming fleet commands. Default is
// **opt-in** because the OSS server doesn't sign commands at all (the
// signing infra is reserved for enterprise gateways that proxy commands
// through a signing layer). Setting MEMCLAW_API_KEY for tenant auth
// shouldn't auto-trigger strict signature requirements that the server
// can't satisfy — that would silently break educate / deploy /
// install_skill / uninstall_skill on every OSS install with auth on.
//
// When this flag is **false** (default): unsigned commands are accepted
// but logged with a one-time warning per process, and any command that
// DOES carry a signature is still verified end-to-end (so a tampered
// signature still fails). When **true**: missing-or-invalid signatures
// fail closed (the original strict behavior).
export const MEMCLAW_REQUIRE_SIGNED_COMMANDS =
  readEnv(["CAURA_REQUIRE_SIGNED_COMMANDS", "MEMCLAW_REQUIRE_SIGNED_COMMANDS"]) === "true";  // legacy-name-ok: rule 3 dual-read alias

// Interviewer Phase 1 opt-in. Default OFF: enabling starts writing the
// node's conversation events to the durable on-disk interview buffer
// (~/.openclaw/plugins/memclaw/interview-buffer.jsonl) — a footprint /
// privacy change an operator must choose, mirroring the server-side
// per-tenant ``interviewer.enabled`` flag. Both must be on for the
// feature to function end-to-end.
export const MEMCLAW_INTERVIEWER = readEnv(["CAURA_INTERVIEWER", "MEMCLAW_INTERVIEWER"]) === "true";  // legacy-name-ok: rule 3 dual-read alias

// Interviewer Phase 1.5 (issue #654): mirror OpenClaw's ``task_runs``
// SQLite trail into the interview buffer at interview time, so task /
// sub-agent-driven work is interviewed, not just the host chat stream.
// Default ON whenever the interviewer itself is on — this is the fix
// for the empty-interview gap, not a separate feature. ``"false"`` is
// the escape hatch if a node's task DB misbehaves.
export const MEMCLAW_INTERVIEWER_TASKS = readEnv(["CAURA_INTERVIEWER_TASKS", "MEMCLAW_INTERVIEWER_TASKS"]) !== "false";  // legacy-name-ok: rule 3 dual-read alias

// Operator override for the task_runs database location. Normally
// discovered (legacy <base>/tasks/runs.sqlite, else a shallow scan for
// a task_runs table — the >= 2026.6 consolidated state DB has no stable
// filename); set this when a deployment relocates OpenClaw state.
export const MEMCLAW_TASK_DB_PATH = readEnv(["CAURA_TASK_DB_PATH", "MEMCLAW_TASK_DB_PATH"]) || "";  // legacy-name-ok: rule 3 dual-read alias

// Warn at import time if API key is set but URL is HTTP
warnIfInsecureUrl(MEMCLAW_API_URL, MEMCLAW_API_KEY);

// --- Tenant resolution ---

/**
 * Per-attempt wall-clock ceiling on the ``/auth/verify`` fetch in
 * ``resolveTenantId``. Without this, a backend that accepts the TCP
 * connection but never replies (slow proxy, half-open conn, etc.) hangs
 * the call forever — and because ``ensureTenantId`` is on the hot path
 * of every lifecycle hook (``ingest`` / ``assemble`` / ``afterTurn``)
 * via the memoized ``_tenantPromise``, every concurrent caller stalls
 * behind the one in-flight fetch. Observed downstream as a
 * ``stalled_agent_run`` (``embedded_run age=156s, queueDepth=4``)
 * diagnostic from OpenClaw on a customer install. The retry loop's
 * ``TypeError`` short-circuit only catches DNS / ECONNREFUSED — accepted
 * but unanswered connections require a deadline. (CAURA-000)
 *
 * 10s matches the other raw-fetch sites (``agent-auth.ts`` provision,
 * ``heartbeat.ts`` manifest) so the plugin's outbound stack has a
 * consistent worst-case per-attempt latency.
 */
const TENANT_RESOLVE_TIMEOUT_MS = 10_000;

export async function resolveTenantId(): Promise<string> {
  if (MEMCLAW_TENANT_ID) return MEMCLAW_TENANT_ID;
  if (!MEMCLAW_API_KEY) return "";

  const MAX_RETRIES = 3;
  const BASE_DELAY_MS = 2000;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const res = await fetch(
        new URL(`${MEMCLAW_API_PREFIX}/auth/verify`, MEMCLAW_API_URL).toString(),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: MEMCLAW_API_KEY }),
          // Bound per-attempt wall-clock; see TENANT_RESOLVE_TIMEOUT_MS
          // docstring above for why this is critical to liveness.
          signal: AbortSignal.timeout(TENANT_RESOLVE_TIMEOUT_MS),
        },
      );
      if (res.ok) {
        const data = (await res.json()) as Record<string, unknown>;
        if (data.tenant_id && typeof data.tenant_id === "string") {
          MEMCLAW_TENANT_ID = data.tenant_id;
          return data.tenant_id;
        }
        console.warn(
          `[caura] tenant_id resolution failed: server returned 200 but response lacks tenant_id field`,
        );
        break; // permanent server-side issue; retrying won't help
      } else if (res.status >= 400 && res.status < 500) {
        console.error(
          `[caura] tenant_id resolution failed: HTTP ${res.status} (client error, not retrying)`,
        );
        break;
      } else if (attempt < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt);
        console.warn(
          `[caura] tenant_id resolution attempt ${attempt + 1}/${MAX_RETRIES + 1} failed: HTTP ${res.status} — retrying in ${delay}ms`,
        );
        await new Promise((r) => setTimeout(r, delay));
      } else {
        console.error(
          `[caura] tenant_id resolution failed after ${MAX_RETRIES + 1} attempts: HTTP ${res.status}`,
        );
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      // undici wraps network-level failures (DNS, ECONNREFUSED, TLS) as
      // TypeError("fetch failed"). These don't heal with backoff — if the
      // backend is unreachable now, it'll be unreachable 14s from now too.
      // Short-circuit with one clear line instead of four noisy retries.
      // This is the common OSS/standalone case: API key set but no backend
      // running (or MEMCLAW_API_URL points at something unreachable).
      if (e instanceof TypeError) {
        console.warn(
          `[memclaw] tenant_id resolution skipped: ${msg} (backend at ${MEMCLAW_API_URL} unreachable; set MEMCLAW_TENANT_ID in .env to run in standalone mode)`,
        );
        break;
      }
      // Our own per-attempt timeout fired (TENANT_RESOLVE_TIMEOUT_MS).
      // The connection was accepted but the server didn't respond in
      // time — distinct from "unreachable" (TypeError above). Worth a
      // clearer log line so operators don't chase phantom DNS /
      // network issues. Retry path unchanged: a transient slow response
      // could heal on the next attempt.
      //
      // ``AbortSignal.timeout()`` aborts with a DOMException whose name
      // is ``"TimeoutError"``. Some platforms / older Node versions may
      // surface plain ``"AbortError"`` — accept both. (Confirmed against
      // Node 22 on the wet-test VM: name is ``"TimeoutError"``.)
      const isTimeout =
        e instanceof Error &&
        (e.name === "TimeoutError" || e.name === "AbortError");
      const reason = isTimeout
        ? `timed out after ${TENANT_RESOLVE_TIMEOUT_MS}ms (backend at ${MEMCLAW_API_URL} accepted the connection but did not respond)`
        : msg;
      if (attempt < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt);
        console.warn(
          `[caura] tenant_id resolution attempt ${attempt + 1}/${MAX_RETRIES + 1} failed: ${reason} — retrying in ${delay}ms`,
        );
        await new Promise((r) => setTimeout(r, delay));
      } else {
        console.error(
          `[caura] tenant_id resolution failed after ${MAX_RETRIES + 1} attempts: ${reason}`,
        );
      }
    }
  }
  return "";
}

let _tenantPromise: Promise<string> | null = null;

export async function ensureTenantId(): Promise<string> {
  if (MEMCLAW_TENANT_ID) return MEMCLAW_TENANT_ID;
  if (!_tenantPromise) {
    _tenantPromise = resolveTenantId();
  }
  const tid = await _tenantPromise;
  if (!tid) {
    _tenantPromise = null;
    throw new Error(
      "MemClaw: Failed to resolve tenant_id from API key. Set MEMCLAW_TENANT_ID in .env.",
    );
  }
  return tid;
}

// --- Tool descriptions ---

let toolDescriptions: Record<string, string> = {};

export async function fetchToolDescriptions(): Promise<void> {
  try {
    const headers: Record<string, string> = {};
    if (MEMCLAW_API_KEY) headers["X-API-Key"] = MEMCLAW_API_KEY;
    const res = await fetch(
      new URL(`${MEMCLAW_API_PREFIX}/tool-descriptions`, MEMCLAW_API_URL).toString(),
      // Bound the fetch — same rationale as resolveTenantId. Less
      // critical here (cold path, called at registration not per-turn)
      // but a hung registration still blocks plugin load.
      { headers, signal: AbortSignal.timeout(TENANT_RESOLVE_TIMEOUT_MS) },
    );
    if (res.ok) {
      toolDescriptions = (await res.json()) as Record<string, string>;
    }
  } catch {
    console.warn("[caura] Failed to fetch tool descriptions, using defaults");
    toolDescriptions = {
      remember: "Store a memory for future retrieval",
      recall: "Search and retrieve relevant memories",
      forget: "Delete a specific memory",
      search: "Search memories by query",
      ingest: "Ingest content from a URL or document",
    };
  }
}

export function getToolDescription(name: string, fallback: string): string {
  return toolDescriptions[name] || fallback;
}

// --- Constants ---

export const HEARTBEAT_INTERVAL_MS = 60_000;
export const HEARTBEAT_INITIAL_DELAY_MS = 5_000;
export const BUILD_TIMEOUT_MS = 30_000;
export const MAX_SOURCE_SIZE = 512_000;
export const RECALL_CACHE_TTL_MS = 60_000;
export const RECALL_TIMEOUT_MS = 10_000;
export const MIN_TURN_CONTENT_LENGTH = 100;
export const MAX_TURN_SUMMARY_LENGTH = 500;
export const MAX_RECALL_CONTENT_LENGTH = 300;

// --- Interviewer (Phase 1): node-local durable event buffer + the ---
// --- interview_request command handler. Dark by default: without   ---
// --- the opt-in flag the plugin writes nothing to disk and answers ---
// --- interview_request with a failed/disabled result.              ---
// Caps mirror the server contract (core_api/constants.py INTERVIEW_*):
// the submit endpoint 422s above these, so truncate at append time.
export const INTERVIEW_SUBMIT_MAX_EVENTS = 500;
export const INTERVIEW_EVENT_MAX_CHARS = 8_000;
export const INTERVIEW_FIELD_MAX_CHARS = 200;
export const INTERVIEW_BUFFER_MAX_BYTES = 50_000_000;
// The submit call runs the server's full map-reduce LLM interview
// SYNCHRONOUSLY — a realistic window is several sequential LLM calls,
// far beyond transport's 15s default (found in the real-LLM pilot:
// the default timeout aborted every full-window submit). Generous
// like BUILD_TIMEOUT_MS; a timeout still fails safe (no prune).
export const INTERVIEW_SUBMIT_TIMEOUT_MS = 300_000;
// Task-trail sync (Phase 1.5): at most this many task events are
// appended per interview tick — bounds the first-sync burst on a busy
// node; the sidecar only advances for emitted rows, so the remainder
// drains on later ticks.
export const INTERVIEW_TASK_SYNC_MAX_PER_TICK = 200;
// Sidecar entries older than this are pruned. One day past OpenClaw's
// 7-day terminal-task retention (task-retention.ts) — anything older
// can never resurface from the DB, so tracking it is waste.
export const INTERVIEW_TASK_SIDECAR_RETENTION_MS = 8 * 24 * 60 * 60_000;

// --- Keystones (CAURA-000): mandatory governance rules auto-injected ---
//
// The ContextEngine fetches keystones from ``/memclaw/keystones`` and
// prepends them to every system prompt. Operators get three knobs:
//
// - ``MEMCLAW_KEYSTONES_ENABLED`` (default ``"true"``) — kill switch so
//   ops can disable the auto-inject without redeploying if something
//   misfires. Set to ``"false"`` to turn it off.
// - ``MEMCLAW_KEYSTONES_TOKEN_CAP`` (default 1500 tokens, ~6000 chars)
//   — hard ceiling on the injected block. Lowest-weight rules are
//   dropped first when the cap is hit so a runaway rule set can't crowd
//   out recall or the operator prompt. The default of 1500 comfortably
//   fits ~20-30 medium-length rules (~120 chars each) with header /
//   footer / truncation-reserve overhead; operators with very large
//   rule sets can raise it further, and operators on small-context
//   models can lower it. Previously 500 — bumped after a customer
//   with 16 rules saw 4 dropped at every turn (CAURA-000).
// - ``MEMCLAW_KEYSTONES_CACHE_TTL_MS`` (default 5 minutes) — per-identity
//   cache TTL. ``caura_keystones_set`` invocations bust the cache for
//   the current session so a freshly authored rule takes effect on the
//   next turn.
function _readBoolEnv(names: readonly string[], defaultValue: boolean): boolean {
  const v = readEnv(names);
  if (v === undefined) return defaultValue;
  // Treat the standard set of "off" idioms as off: ``"false"``, ``"0"``,
  // the empty string, ``"no"``, ``"off"``, and ``"disabled"``. The last
  // three cover the common shell-script operator idioms that show up in
  // ``.env`` files in the wild (``FLAG=no``, ``FLAG=off``,
  // ``FLAG=disabled``). Anything else (including ``"true"``, ``"1"``,
  // ``"yes"``, or unrecognised values) is treated as on so operators can
  // opt-in with whatever convention their shell uses. The empty-string
  // case matters because ``KEY=`` in an ``.env`` file is the natural way
  // to "blank out" a previously-set value.
  const lower = v.toLowerCase();
  return (
    lower !== "false" &&
    lower !== "0" &&
    lower !== "" &&
    lower !== "no" &&
    lower !== "off" &&
    lower !== "disabled"
  );
}
function _readIntEnv(names: readonly string[], defaultValue: number, min: number): number {
  const raw = readEnv(names);
  if (!raw) return defaultValue;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < min) return defaultValue;
  return n;
}
export const MEMCLAW_KEYSTONES_ENABLED: boolean = _readBoolEnv(
  ["CAURA_KEYSTONES_ENABLED", "MEMCLAW_KEYSTONES_ENABLED"],  // legacy-name-ok: rule 3 dual-read alias
  true,
);
export const MEMCLAW_KEYSTONES_TOKEN_CAP: number = _readIntEnv(
  ["CAURA_KEYSTONES_TOKEN_CAP", "MEMCLAW_KEYSTONES_TOKEN_CAP"],  // legacy-name-ok: rule 3 dual-read alias
  1500,
  1,
);
export const MEMCLAW_KEYSTONES_CACHE_TTL_MS: number = _readIntEnv(
  ["CAURA_KEYSTONES_CACHE_TTL_MS", "MEMCLAW_KEYSTONES_CACHE_TTL_MS"],  // legacy-name-ok: rule 3 dual-read alias
  300_000,
  1_000,
);
export const KEYSTONES_TIMEOUT_MS = 5_000;

// --- Recall policy (gates ContextEngine.assemble's /search call) ---
//
// The OpenClaw runtime calls our context engine on every prompt assembly
// (heartbeats, tool follow-ups, no-reply lurk turns, trivial pings — all
// of them). Without gating, every call hits the Caura backend with a
// `/search` regardless of whether the turn would benefit from LTM. These
// knobs let operators tune when recall fires.

export type RecallPolicy = "auto" | "always" | "never" | "keywords";

const _validPolicies: ReadonlySet<RecallPolicy> = new Set([
  "auto",
  "always",
  "never",
  "keywords",
]);

function _readPolicy(): RecallPolicy {
  if (readEnv(["CAURA_RECALL_FORCE", "MEMCLAW_RECALL_FORCE"]) === "true") return "always";  // legacy-name-ok: rule 3 dual-read alias
  const raw = (readEnv(["CAURA_RECALL_POLICY", "MEMCLAW_RECALL_POLICY"]) || "auto").toLowerCase();  // legacy-name-ok: rule 3 dual-read alias
  return _validPolicies.has(raw as RecallPolicy)
    ? (raw as RecallPolicy)
    : "auto";
}

function _readMinPromptChars(): number {
  const raw = parseInt(readEnv(["CAURA_RECALL_MIN_PROMPT_CHARS", "MEMCLAW_RECALL_MIN_PROMPT_CHARS"]) || "", 10);  // legacy-name-ok: rule 3 dual-read alias
  return Number.isFinite(raw) && raw >= 0 ? raw : 14;
}

const DEFAULT_TRIGGER_KEYWORDS = [
  "memclaw",
  "ltm",
  "long term",
  "long-term",
  "remember",
  "recall",
  "what did",
  "earlier",
  "previously",
  "last time",
  "before",
  "we discussed",
  "you said",
  "i told",
  "history",
  "memory",
  "lookup",
] as const;

function _readTriggerKeywords(): readonly string[] {
  const raw = readEnv(["CAURA_RECALL_TRIGGER_KEYWORDS", "MEMCLAW_RECALL_TRIGGER_KEYWORDS"]);  // legacy-name-ok: rule 3 dual-read alias
  if (!raw) return DEFAULT_TRIGGER_KEYWORDS;
  const tokens = raw
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter((t) => t.length > 0);
  return tokens.length > 0 ? tokens : DEFAULT_TRIGGER_KEYWORDS;
}

function _readDenySessions(): readonly string[] {
  const raw = readEnv(["CAURA_RECALL_DENY_SESSIONS", "MEMCLAW_RECALL_DENY_SESSIONS"]);  // legacy-name-ok: rule 3 dual-read alias
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

// Automation / machine-turn patterns. When a turn matches one of these it is
// an agent's own heartbeat/cron/health-check/status line, not an
// information-seeking question — recalling on it just injects noise. The set
// is DEPLOYMENT-TUNABLE (a term that's noise here may be real business content
// elsewhere, e.g. "health check" for an ops product), so operators can
// override the whole list via MEMCLAW_RECALL_MACHINE_PATTERNS (comma-separated
// regex sources). Defaults are seeded from observed eToro automation traffic.
const DEFAULT_MACHINE_PATTERNS = [
  "heartbeat",
  "\\bcron\\b",
  "health ?check",
  "checkpoint",
  "\\bpm2\\b",
  "disk \\d",
  "rpc ok",
  "watchdog",
  "status\\.md",
  "no commits",
  "quiet.?hours",
  "no active",
  "sync block",
  "cache refresh",
] as const;

function _readMachinePatterns(): readonly RegExp[] {
  const raw = readEnv(["CAURA_RECALL_MACHINE_PATTERNS", "MEMCLAW_RECALL_MACHINE_PATTERNS"]);  // legacy-name-ok: rule 3 dual-read alias
  const sources = raw
    ? raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0)
    : [...DEFAULT_MACHINE_PATTERNS];
  const out: RegExp[] = [];
  for (const s of sources) {
    try {
      out.push(new RegExp(s, "i"));
    } catch {
      // Skip an invalid operator-supplied pattern rather than crash the plugin.
    }
  }
  return out;
}

export const RECALL_POLICY: RecallPolicy = _readPolicy();
export const RECALL_MIN_PROMPT_CHARS: number = _readMinPromptChars();
export const RECALL_TRIGGER_KEYWORDS: readonly string[] = _readTriggerKeywords();
export const RECALL_DENY_SESSIONS: readonly string[] = _readDenySessions();
export const RECALL_MACHINE_PATTERNS: readonly RegExp[] = _readMachinePatterns();

// Noise-skip gate rollout mode (MEMCLAW_RECALL_GATE):
//   "off"    (default) — new noise-skip rules (machine / agent-name /
//              mention-only / subagent-context / 3rd-party-instruction) are
//              NOT applied. Merge is behavior-neutral; opt-in required.
//   "shadow" — rules are evaluated and LOGGED as "would-skip" but do NOT
//              suppress recall. Lets operators measure impact first.
//   "on"     — rules are enforced (recall suppressed on a match).
// Default "off" so shipping this change does not alter recall behavior for any
// deployment until an operator opts in (cross-customer safety not yet validated).
export type RecallGateMode = "off" | "shadow" | "on";
function _readGateMode(): RecallGateMode {
  const raw = (readEnv(["CAURA_RECALL_GATE", "MEMCLAW_RECALL_GATE"]) || "off").toLowerCase();  // legacy-name-ok: rule 3 dual-read alias
  return raw === "shadow" || raw === "on" ? raw : "off";
}
export const RECALL_GATE_MODE: RecallGateMode = _readGateMode();
// Cross-agent recall: when true, auto-recall omits filter_agent_id so the
// agent can retrieve knowledge authored by SIBLING agents (still bounded by
// the server-side fleet/trust scope). Default false — this changes read
// isolation and must ship alongside the freshness cap (A43), else a
// future-dated hub memory dominates cross-agent results. Opt-in.
export const RECALL_CROSS_AGENT: boolean = _readBoolEnv(["CAURA_RECALL_CROSS_AGENT", "MEMCLAW_RECALL_CROSS_AGENT"], false);  // legacy-name-ok: rule 3 dual-read alias
