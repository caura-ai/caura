/**
 * Environment configuration and tenant resolution for MemClaw plugin.
 *
 * Security fixes:
 * - .env parse errors are logged (no silent swallow)
 * - HTTPS warning on insecure URL
 */

import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";
import { warnIfInsecureUrl } from "./validation.js";

// --- Load .env file from plugin directory ---
try {
  const envPath = join(homedir(), ".openclaw", "plugins", "memclaw", ".env");
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
      // Only set MEMCLAW_* vars — prevent .env from hijacking PATH, NODE_OPTIONS, etc.
      if (!/^MEMCLAW_[A-Z_]+$/.test(key)) continue;
      process.env[key] = val;
    }
  }
} catch (e: unknown) {
  const msg = e instanceof Error ? e.message : String(e);
  console.warn("[memclaw] Failed to parse .env file:", msg);
}

export const MEMCLAW_API_URL =
  process.env.MEMCLAW_API_URL || "http://localhost:8000";

/**
 * Prefix for all MemClaw REST routes. Single source of truth for API
 * versioning — bump to "/api/v2" here when the backend ships a new version.
 *
 * The transport layer auto-prepends this to relative paths. Raw fetch
 * sites use it via template literal.
 */
export const MEMCLAW_API_PREFIX = process.env.MEMCLAW_API_PREFIX || "/api/v1";
export const MEMCLAW_API_KEY = process.env.MEMCLAW_API_KEY || "";
export const MEMCLAW_FLEET_ID = process.env.MEMCLAW_FLEET_ID || "";
export let MEMCLAW_TENANT_ID = process.env.MEMCLAW_TENANT_ID || "";
export const MEMCLAW_NODE_NAME = process.env.MEMCLAW_NODE_NAME || "";
export const MEMCLAW_AGENT_ID = process.env.MEMCLAW_AGENT_ID || "";
// Default to true — auto-writing turn summaries is the core fix for the
// "100% dark matter" problem (memories written but never recalled).
// Users can opt out with MEMCLAW_AUTO_WRITE_TURNS=false.
export const MEMCLAW_AUTO_WRITE_TURNS =
  process.env.MEMCLAW_AUTO_WRITE_TURNS !== "false";

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
  process.env.MEMCLAW_REQUIRE_SIGNED_COMMANDS === "true";

// Warn at import time if API key is set but URL is HTTP
warnIfInsecureUrl(MEMCLAW_API_URL, MEMCLAW_API_KEY);

// --- Tenant resolution ---

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
        },
      );
      if (res.ok) {
        const data = (await res.json()) as Record<string, unknown>;
        if (data.tenant_id && typeof data.tenant_id === "string") {
          MEMCLAW_TENANT_ID = data.tenant_id;
          return data.tenant_id;
        }
        console.warn(
          `[memclaw] tenant_id resolution failed: server returned 200 but response lacks tenant_id field`,
        );
        break; // permanent server-side issue; retrying won't help
      } else if (res.status >= 400 && res.status < 500) {
        console.error(
          `[memclaw] tenant_id resolution failed: HTTP ${res.status} (client error, not retrying)`,
        );
        break;
      } else if (attempt < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt);
        console.warn(
          `[memclaw] tenant_id resolution attempt ${attempt + 1}/${MAX_RETRIES + 1} failed: HTTP ${res.status} — retrying in ${delay}ms`,
        );
        await new Promise((r) => setTimeout(r, delay));
      } else {
        console.error(
          `[memclaw] tenant_id resolution failed after ${MAX_RETRIES + 1} attempts: HTTP ${res.status}`,
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
      if (attempt < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt);
        console.warn(
          `[memclaw] tenant_id resolution attempt ${attempt + 1}/${MAX_RETRIES + 1} failed: ${msg} — retrying in ${delay}ms`,
        );
        await new Promise((r) => setTimeout(r, delay));
      } else {
        console.error(
          `[memclaw] tenant_id resolution failed after ${MAX_RETRIES + 1} attempts: ${msg}`,
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
      { headers },
    );
    if (res.ok) {
      toolDescriptions = (await res.json()) as Record<string, string>;
    }
  } catch {
    console.warn("[memclaw] Failed to fetch tool descriptions, using defaults");
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

// --- Keystones (CAURA-000): mandatory governance rules auto-injected ---
//
// The ContextEngine fetches keystones from ``/memclaw/keystones`` and
// prepends them to every system prompt. Operators get three knobs:
//
// - ``MEMCLAW_KEYSTONES_ENABLED`` (default ``"true"``) — kill switch so
//   ops can disable the auto-inject without redeploying if something
//   misfires. Set to ``"false"`` to turn it off.
// - ``MEMCLAW_KEYSTONES_TOKEN_CAP`` (default 500 tokens, ~2000 chars)
//   — hard ceiling on the injected block. Lowest-weight rules are
//   dropped first when the cap is hit so a runaway rule set can't crowd
//   out recall or the operator prompt.
// - ``MEMCLAW_KEYSTONES_CACHE_TTL_MS`` (default 5 minutes) — per-identity
//   cache TTL. ``memclaw_keystones_set`` invocations bust the cache for
//   the current session so a freshly authored rule takes effect on the
//   next turn.
function _readBoolEnv(name: string, defaultValue: boolean): boolean {
  const v = process.env[name];
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
function _readIntEnv(name: string, defaultValue: number, min: number): number {
  const raw = process.env[name];
  if (!raw) return defaultValue;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < min) return defaultValue;
  return n;
}
export const MEMCLAW_KEYSTONES_ENABLED: boolean = _readBoolEnv(
  "MEMCLAW_KEYSTONES_ENABLED",
  true,
);
export const MEMCLAW_KEYSTONES_TOKEN_CAP: number = _readIntEnv(
  "MEMCLAW_KEYSTONES_TOKEN_CAP",
  500,
  1,
);
export const MEMCLAW_KEYSTONES_CACHE_TTL_MS: number = _readIntEnv(
  "MEMCLAW_KEYSTONES_CACHE_TTL_MS",
  300_000,
  1_000,
);
export const KEYSTONES_TIMEOUT_MS = 5_000;

// --- Recall policy (gates ContextEngine.assemble's /search call) ---
//
// The OpenClaw runtime calls our context engine on every prompt assembly
// (heartbeats, tool follow-ups, no-reply lurk turns, trivial pings — all
// of them). Without gating, every call hits the MemClaw backend with a
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
  if (process.env.MEMCLAW_RECALL_FORCE === "true") return "always";
  const raw = (process.env.MEMCLAW_RECALL_POLICY || "auto").toLowerCase();
  return _validPolicies.has(raw as RecallPolicy)
    ? (raw as RecallPolicy)
    : "auto";
}

function _readMinPromptChars(): number {
  const raw = parseInt(process.env.MEMCLAW_RECALL_MIN_PROMPT_CHARS || "", 10);
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
  const raw = process.env.MEMCLAW_RECALL_TRIGGER_KEYWORDS;
  if (!raw) return DEFAULT_TRIGGER_KEYWORDS;
  const tokens = raw
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter((t) => t.length > 0);
  return tokens.length > 0 ? tokens : DEFAULT_TRIGGER_KEYWORDS;
}

function _readDenySessions(): readonly string[] {
  const raw = process.env.MEMCLAW_RECALL_DENY_SESSIONS;
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export const RECALL_POLICY: RecallPolicy = _readPolicy();
export const RECALL_MIN_PROMPT_CHARS: number = _readMinPromptChars();
export const RECALL_TRIGGER_KEYWORDS: readonly string[] = _readTriggerKeywords();
export const RECALL_DENY_SESSIONS: readonly string[] = _readDenySessions();
