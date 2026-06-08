/**
 * Agent authentication broker for MemClaw plugin.
 *
 * Auto-provisions agent-scoped credentials using the tenant-scoped key
 * as a bootstrap credential. Both kinds share the `mc_` prefix on the
 * wire; the credential's kind (tenant vs agent) is bound at mint time.
 * Keys are cached in memory and persisted to a local secrets file
 * (mode 600).
 *
 * Flow:
 *   1. resolveAgentKey(agentId) checks in-memory cache
 *   2. Falls back to secrets file on disk
 *   3. If missing, provisions via POST /api/v1/admin/agent-keys/provision
 *   4. Stores the raw key and returns it
 *   5. On 401, evicts and re-provisions (see handleAgentAuthError)
 */

import { readFileSync, writeFileSync, existsSync, chmodSync } from "fs";
import { MEMCLAW_API_URL, MEMCLAW_API_KEY, MEMCLAW_API_PREFIX } from "./env.js";
import { getSecretsPath } from "./paths.js";
import { logError } from "./logger.js";

// --- Configuration ---

const SECRETS_PATH = getSecretsPath();

// Skip agent auth when no tenant key or running in standalone/OSS mode
const AGENT_AUTH_ENABLED = Boolean(MEMCLAW_API_KEY);

// --- In-memory cache ---

const keyCache = new Map<string, string>();

// --- Secrets file I/O ---

interface SecretsFile {
  version: 1;
  keys: Record<string, { key: string; prefix: string; provisioned_at: string }>;
}

function readSecretsFile(): SecretsFile {
  try {
    if (existsSync(SECRETS_PATH)) {
      const data = JSON.parse(readFileSync(SECRETS_PATH, "utf-8"));
      if (data?.version === 1 && data?.keys) return data;
    }
  } catch {
    // Corrupt file — start fresh
  }
  return { version: 1, keys: {} };
}

function writeSecretsFile(secrets: SecretsFile): void {
  writeFileSync(SECRETS_PATH, JSON.stringify(secrets, null, 2) + "\n", {
    mode: 0o600,
  });
  try {
    chmodSync(SECRETS_PATH, 0o600);
  } catch {
    // Best-effort on platforms that don't support chmod
  }
}

// --- Provisioning ---

async function provisionAgentKey(
  agentId: string,
): Promise<{ raw_key: string; key_prefix: string } | null> {
  try {
    const url = new URL(`${MEMCLAW_API_PREFIX}/admin/agent-keys/provision`, MEMCLAW_API_URL);
    const res = await fetch(url.toString(), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": MEMCLAW_API_KEY,
      },
      body: JSON.stringify({ agent_id: agentId }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      console.warn(
        `[memclaw] Agent key provisioning failed for '${agentId}': ${res.status}`,
      );
      return null;
    }
    const data = (await res.json()) as { raw_key: string; key_prefix: string };
    console.log(
      `[memclaw] Provisioned agent key for '${agentId}' (${data.key_prefix})`,
    );
    return data;
  } catch (e: unknown) {
    logError(`Agent key provisioning error for '${agentId}'`, e);
    return null;
  }
}

// --- Public API ---

/**
 * Resolve the API key for a specific agent.
 *
 * Returns the agent-scoped credential if available/provisionable,
 * or null to fall back to the tenant-scoped key. Both share the
 * `mc_` wire prefix; the resolved kind drives the X-Agent-ID
 * injection at the gateway.
 */
export async function resolveAgentKey(
  agentId: string,
): Promise<string | null> {
  if (!AGENT_AUTH_ENABLED) return null;
  // Skip synthetic agent IDs
  if (!agentId || agentId === "unknown-agent" || agentId === "__health_check__") {
    return null;
  }

  // 1. In-memory cache
  const cached = keyCache.get(agentId);
  if (cached) return cached;

  // 2. Secrets file
  const secrets = readSecretsFile();
  const entry = secrets.keys[agentId];
  if (entry?.key) {
    keyCache.set(agentId, entry.key);
    return entry.key;
  }

  // 3. Provision
  const result = await provisionAgentKey(agentId);
  if (!result) return null;

  // 4. Persist
  secrets.keys[agentId] = {
    key: result.raw_key,
    prefix: result.key_prefix,
    provisioned_at: new Date().toISOString(),
  };
  writeSecretsFile(secrets);
  keyCache.set(agentId, result.raw_key);

  return result.raw_key;
}

/**
 * Handle a 401 response when using an agent key.
 * Evicts the cached key and re-provisions on next call.
 */
export function evictAgentKey(agentId: string): void {
  keyCache.delete(agentId);
  try {
    const secrets = readSecretsFile();
    if (secrets.keys[agentId]) {
      delete secrets.keys[agentId];
      writeSecretsFile(secrets);
      console.log(`[memclaw] Evicted agent key for '${agentId}' (will re-provision)`);
    }
  } catch {
    // Best-effort eviction
  }
}
