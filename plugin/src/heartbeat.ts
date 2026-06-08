/**
 * Heartbeat loop and command processing.
 *
 * Security fixes:
 * - HMAC signature verification on commands before execution
 * - Safe ID validation on cmd.id before URL interpolation
 * - Prompt length cap on educate commands
 * - Workspace path stripping from telemetry (hash instead)
 */

import {
  readFileSync,
  writeFileSync,
  existsSync,
  readdirSync,
  unlinkSync,
  mkdirSync,
} from "fs";
import { join } from "path";
import { createHash } from "crypto";
import { execSync } from "child_process";
import { hostname, platform, release, networkInterfaces } from "os";
import { getOpenClawBaseDir } from "./paths.js";

import { apiCall } from "./transport.js";
import {
  MEMCLAW_API_URL,
  MEMCLAW_API_PREFIX,
  MEMCLAW_API_KEY,
  MEMCLAW_TENANT_ID,
  MEMCLAW_NODE_NAME,
  MEMCLAW_FLEET_ID,
  MEMCLAW_REQUIRE_SIGNED_COMMANDS,
  BUILD_TIMEOUT_MS,
  MAX_SOURCE_SIZE,
} from "./env.js";
import { PLUGIN_VERSION } from "./version.js";
import { MEMCLAW_TOOLS } from "./tools.js";
import {
  getPluginDir,
  getMissingTools,
  readOpenClawConfig,
  isMemclawFullyConfigured,
} from "./config.js";
import { getReachability, markReachable, markUnreachable } from "./health.js";
import { deployPlugin } from "./deploy.js";
import {
  educateAgents,
  writeEducationFiles,
  buildToolsMd,
  buildAgentsMd,
} from "./educate.js";
import { getRecallMetrics } from "./context-engine.js";
import {
  verifyCommandSignature,
  assertSafePathSegment,
  assertPromptLength,
} from "./validation.js";
import { logError } from "./logger.js";
import { getInstallId } from "./install-id.js";
import { getDisplayName } from "./identity.js";
import { reconcileSkills } from "./reconcile-skills.js";

let heartbeatCount = 0;
let bakCleanupDone = false;
let postRestartCheckDone = false;

// --- Deploy cooldown / post-restart verification (CAURA-444) ---
//
// On deploy success the plugin writes ``.deploy-pending.json`` BEFORE
// triggering the restart. The new process checks this on first
// heartbeat: if PLUGIN_VERSION matches the stamped target, the deploy
// succeeded and the marker is cleared. If it doesn't match (something
// rolled back, prebuild failed silently and version.ts wasn't updated,
// the gateway never restarted, etc.), we record a failure into
// ``.deploy-cooldown.json`` and refuse further deploys for that
// version for ``MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS``.
//
// The plugin also surfaces ``deploy_blocked_until`` in its heartbeat
// payload so the backend's auto-upgrade trigger can skip queueing
// commands during the cooldown window.

const DEPLOY_PENDING_FILE = ".deploy-pending.json";
const DEPLOY_COOLDOWN_FILE = ".deploy-cooldown.json";

function _failureCooldownHours(): number {
  const raw = parseInt(
    process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS || "",
    10,
  );
  return Number.isFinite(raw) && raw > 0 ? raw : 24;
}

/**
 * Schedule a graceful restart of the openclaw-gateway. Module-level
 * helper extracted from three (previously duplicated) inline blocks
 * inside ``processCommand``. Overridable via ``__DEPLOY_INTERNALS__``
 * for tests — the production path runs ``systemctl --user restart``
 * with a fallback ``process.exit(0)`` when systemctl is unavailable.
 *
 * The 2-second delay is intentional grace: it gives any in-flight
 * heartbeat/log/network I/O a chance to drain before SIGTERM. Pre-
 * CAURA-000 the result POST was racing with this delay (POST was
 * issued AFTER the setTimeout was scheduled, so a slow POST got
 * killed mid-flight). The fix moves the restart scheduling to AFTER
 * the result POST resolves, so the 2-second delay now only covers
 * any minor post-POST tidy-up rather than the POST itself.
 *
 * The production implementation is kept in a ``const`` so test seams
 * can RESTORE it via ``setScheduleRestartForTests(null)`` after they
 * swap in a spy — without this an ``afterEach`` could only LEAVE the
 * module in test-spy state, and any subsequent code (in the same
 * process) that reached ``processCommand`` with a restart command
 * would silently skip the systemctl call.
 */
const _originalScheduleGracefulRestart = (): void => {
  setTimeout(() => {
    try {
      execSync("systemctl --user restart openclaw-gateway 2>&1", {
        encoding: "utf-8",
        timeout: 10_000,
      });
    } catch {
      process.exit(0);
    }
  }, 2000);
};

let _scheduleGracefulRestart: () => void = _originalScheduleGracefulRestart;

// Exported for tests. Public surface is intentionally small.
export const __DEPLOY_INTERNALS__ = {
  get DEPLOY_PENDING_FILE() { return DEPLOY_PENDING_FILE; },
  get DEPLOY_COOLDOWN_FILE() { return DEPLOY_COOLDOWN_FILE; },
  failureCooldownHours: _failureCooldownHours,
  readCooldown: () => readDeployCooldown(),
  writeCooldown: (v: string, r: string) => writeDeployCooldown(v, r),
  clearCooldown: () => clearDeployCooldown(),
  readPending: () => readDeployPending(),
  writePending: (v: string) => writeDeployPending(v),
  clearPending: () => clearDeployPending(),
  isBlocked: (v: string) => isDeployBlocked(v),
  // Test-only: resets the postRestartCheckDone guard so a single
  // process can exercise the post-restart verification path more
  // than once. Gated to ``NODE_ENV === "test"`` so a production
  // import surface can't accidentally re-arm the check (which would
  // re-run cooldown writes on a still-running agent).
  resetPostRestartCheck: () => {
    if (process.env.NODE_ENV !== "test") return;
    postRestartCheckDone = false;
  },
  verifyPostRestart: () => verifyDeployPostRestart(),
  // Test-only: drive ``processCommand`` directly and substitute the
  // restart scheduler. The substitution seam exists for CAURA-000
  // race-test coverage — the production restart fires
  // ``systemctl --user restart`` which would kill the test process.
  // Gated to ``NODE_ENV === "test"`` so a production import surface
  // can't accidentally disable real restarts (a "deploy" or "restart"
  // command would then succeed silently — backend would see status=done
  // but the gateway would never actually restart, leaving the node
  // permanently behind).
  processCommand: (cmd: Parameters<typeof processCommand>[0]) =>
    processCommand(cmd),
  // Pass a function to install a spy; pass ``null`` to restore the
  // production scheduler (use in ``afterEach`` so subsequent tests in
  // the same process don't inherit the spy state — without this, a
  // later test that reaches ``processCommand`` with a restart command
  // would silently skip the systemctl call and any assertion downstream
  // would mis-attribute the cause).
  //
  // Calls from outside NODE_ENV=test are REJECTED LOUDLY (warn line):
  // a silent no-op meant a misconfigured test runner would let the real
  // ``systemctl --user restart`` fire 2 seconds AFTER assertions passed,
  // killing the test process and turning a green CI run into a confused
  // post-mortem. The plugin's ``package.json:test`` script already sets
  // NODE_ENV=test; this warn is the safety net for anyone running tests
  // through other harnesses.
  setScheduleRestartForTests: (fn: (() => void) | null) => {
    if (process.env.NODE_ENV !== "test") {
      console.warn(
        "[memclaw] __DEPLOY_INTERNALS__.setScheduleRestartForTests called " +
          "outside NODE_ENV=test — ignored. The production restart scheduler " +
          "is still active; tests using this seam must set NODE_ENV=test " +
          "(the plugin's npm test script already does) or the real " +
          "systemctl --user restart will fire after the test completes.",
      );
      return;
    }
    _scheduleGracefulRestart = fn ?? _originalScheduleGracefulRestart;
  },
};

function readDeployCooldown(): { failed_version?: string; blocked_until?: number } {
  try {
    const path = join(getPluginDir(), DEPLOY_COOLDOWN_FILE);
    if (!existsSync(path)) return {};
    const data = JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>;
    return {
      failed_version:
        typeof data.failed_version === "string" ? data.failed_version : undefined,
      blocked_until:
        typeof data.blocked_until === "number" ? data.blocked_until : undefined,
    };
  } catch {
    return {};
  }
}

function writeDeployCooldown(failed_version: string, reason: string): void {
  try {
    const path = join(getPluginDir(), DEPLOY_COOLDOWN_FILE);
    const blocked_until = Date.now() + _failureCooldownHours() * 3600_000;
    writeFileSync(
      path,
      JSON.stringify({
        failed_version,
        reason,
        ts: new Date().toISOString(),
        blocked_until,
      }) + "\n",
      "utf-8",
    );
    console.warn(
      `[memclaw] deploy cooldown engaged: failed_version=${failed_version} ` +
        `reason=${reason} blocked_until=${new Date(blocked_until).toISOString()}`,
    );
  } catch (e: unknown) {
    logError("Failed to write deploy cooldown", e);
  }
}

function clearDeployCooldown(): void {
  try {
    const path = join(getPluginDir(), DEPLOY_COOLDOWN_FILE);
    if (existsSync(path)) unlinkSync(path);
  } catch {
    // Best-effort
  }
}

function readDeployPending(): { target_version?: string; ts?: string } {
  try {
    const path = join(getPluginDir(), DEPLOY_PENDING_FILE);
    if (!existsSync(path)) return {};
    // Explicit per-field validation mirrors readDeployCooldown.
    // Without this, a corrupted / hand-edited .deploy-pending.json with
    // non-string target_version would silently flow into
    // verifyDeployPostRestart's `pending.target_version === PLUGIN_VERSION`
    // check and either falsely succeed (number coercion) or short-circuit
    // out via `if (!pending.target_version)` — never engaging the
    // cooldown the file was supposed to track.
    const data = JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>;
    return {
      target_version:
        typeof data.target_version === "string" ? data.target_version : undefined,
      ts: typeof data.ts === "string" ? data.ts : undefined,
    };
  } catch {
    return {};
  }
}

function clearDeployPending(): void {
  try {
    const path = join(getPluginDir(), DEPLOY_PENDING_FILE);
    if (existsSync(path)) unlinkSync(path);
  } catch {
    // Best-effort
  }
}

function writeDeployPending(target_version: string): void {
  try {
    const path = join(getPluginDir(), DEPLOY_PENDING_FILE);
    writeFileSync(
      path,
      JSON.stringify({ target_version, ts: new Date().toISOString() }) + "\n",
      "utf-8",
    );
  } catch (e: unknown) {
    logError("Failed to write deploy-pending marker", e);
  }
}

/**
 * On first heartbeat after process start, check whether a deploy was
 * pending. If yes and we're on the target version → success, clear the
 * marker. If yes and we're NOT on the target → failure, write cooldown.
 */
function verifyDeployPostRestart(): void {
  if (postRestartCheckDone) return;
  postRestartCheckDone = true;
  const pending = readDeployPending();
  if (!pending.target_version) return;
  if (pending.target_version === PLUGIN_VERSION) {
    console.log(
      `[memclaw] deploy verified post-restart: now running v${PLUGIN_VERSION}`,
    );
    clearDeployPending();
    clearDeployCooldown();
  } else {
    console.error(
      `[memclaw] DEPLOY VERIFICATION FAILED: target=${pending.target_version} ` +
        `actual=${PLUGIN_VERSION} — engaging cooldown`,
    );
    writeDeployCooldown(
      pending.target_version,
      "post-restart-version-mismatch",
    );
    clearDeployPending();
  }
}

/** Should we accept a fresh deploy command for this target version? */
function isDeployBlocked(target_version: string): { blocked: boolean; until?: number } {
  const cd = readDeployCooldown();
  if (
    cd.failed_version === target_version &&
    cd.blocked_until &&
    cd.blocked_until > Date.now()
  ) {
    return { blocked: true, until: cd.blocked_until };
  }
  return { blocked: false };
}

function cleanupStaleBackups(): void {
  if (bakCleanupDone) return;
  bakCleanupDone = true;
  try {
    const srcDir = join(getPluginDir(), "src");
    if (!existsSync(srcDir)) return;
    for (const f of readdirSync(srcDir)) {
      if (f.endsWith(".bak")) {
        try {
          unlinkSync(join(srcDir, f));
        } catch {
          // Ignore per-file errors
        }
      }
    }
  } catch {
    // Cleanup is best-effort
  }
}

export async function sendHeartbeat(): Promise<void> {
  cleanupStaleBackups();
  verifyDeployPostRestart();
  if (!MEMCLAW_TENANT_ID || !MEMCLAW_NODE_NAME) return;

  // Skill reconciler — converge plugin/skills/ with the catalog before
  // anything else. Failures are non-fatal (best-effort distribution).
  // Replaces the dropped install_skill / uninstall_skill push commands.
  try {
    await reconcileSkills();
  } catch (e: unknown) {
    logError("reconcileSkills failed", e);
  }

  const pluginDir = getPluginDir();

  // Get primary IP address
  let ipAddress: string | undefined;
  try {
    const nets = networkInterfaces();
    for (const name of Object.keys(nets)) {
      for (const iface of nets[name] || []) {
        if (iface.family === "IPv4" && !iface.internal) {
          ipAddress = iface.address;
          break;
        }
      }
      if (ipAddress) break;
    }
  } catch {
    // Network interfaces unavailable
  }

  // Get OpenClaw version
  let openclawVersion: string | undefined;
  try {
    const ver = execSync("openclaw --version 2>/dev/null || echo unknown", {
      encoding: "utf-8",
      timeout: 3000,
    }).trim();
    if (ver && ver !== "unknown") openclawVersion = ver;
  } catch {
    // openclaw CLI not available
  }

  const srcPath = join(pluginDir, "src", "index.ts");
  let pluginHash: string | undefined;
  try {
    if (existsSync(srcPath)) {
      pluginHash = createHash("sha256")
        .update(readFileSync(srcPath, "utf-8"), "utf-8")
        .digest("hex");
    }
  } catch {
    // Source file read failed
  }

  // Collect agents from config — hash workspace paths instead of sending them raw
  // Per-install opaque suffix used to disambiguate the default ``"main"``
  // agent across multiple plugin installs sharing one tenant. Generated
  // once at first heartbeat and persisted to ``install.json``.
  const installId = getInstallId();

  let agents: Array<Record<string, unknown>> | undefined;
  try {
    const config = readOpenClawConfig() as Record<string, any> | null;
    const agentList = config?.agents?.list;
    if (Array.isArray(agentList) && agentList.length > 0) {
      // Operators who configured explicit ``agents.list`` keep their
      // chosen ids verbatim. ``display_name`` defaults to the
      // hostname-prefixed form when the entry has no explicit
      // ``display_name``.
      agents = agentList.map((a: Record<string, any>) => {
        const baseName = a.id || a.name || "unknown";
        return {
          agentId: baseName,
          name: a.name || a.id || "unknown",
          display_name:
            typeof a.display_name === "string" && a.display_name
              ? a.display_name
              : getDisplayName(baseName),
          workspace_hash: a.workspace
            ? createHash("sha256").update(a.workspace).digest("hex").slice(0, 12)
            : undefined,
          model: a.model?.primary || config?.agents?.defaults?.model?.primary || undefined,
          tools_profile: a.tools?.profile || undefined,
        };
      });
    } else {
      // No explicit list — synthesize a single default agent. Pre-Task6
      // this was hardcoded to ``"main"`` and collided with every other
      // install. Now the internal id carries the install suffix; the
      // human label uses the hostname.
      const defaultModel = config?.agents?.defaults?.model?.primary;
      agents = [
        {
          agentId: `main-${installId}`,
          name: getDisplayName("main"),
          display_name: getDisplayName("main"),
          model: defaultModel || undefined,
        },
      ];
    }
  } catch {
    // Config read failed
  }

  // Build setup_status for Fleet UI completeness indicator
  let setupStatus: Record<string, unknown> | undefined;
  try {
    const config = readOpenClawConfig() as Record<string, any> | null;
    const toolsAllowed = config
      ? getMissingTools(config).length === 0
      : false;
    const educated = existsSync(join(getPluginDir(), ".educated"));

    // Check workspace files for MemClaw references. SKILL.md is no longer a
    // per-workspace artifact — it ships at the plugin root and is discovered
    // by OpenClaw via `openclaw.plugin.json:skills`. The shared skill file's
    // presence is checked once below and reported on setup_status.
    const workspaceFiles: Record<string, Record<string, boolean>> = {};
    const ocBase = getOpenClawBaseDir();
    try {
      const entries = readdirSync(ocBase);
      for (const d of entries) {
        if (!d.startsWith("workspace")) continue;
        const wsPath = join(ocBase, d);
        try {
          const hb =
            existsSync(join(wsPath, "HEARTBEAT.md")) &&
            readFileSync(join(wsPath, "HEARTBEAT.md"), "utf-8").includes("memclaw");
          const tools =
            existsSync(join(wsPath, "TOOLS.md")) &&
            readFileSync(join(wsPath, "TOOLS.md"), "utf-8").toLowerCase().includes("memclaw");
          workspaceFiles[d] = { heartbeat_md: !!hb, tools_md: !!tools };
        } catch {
          // Skip workspace on error
        }
      }
    } catch {
      // Base dir read failed
    }

    // Shared plugin skill file — checked once, reported on setup_status.
    const sharedSkillPath = join(getPluginDir(), "skills", "memclaw", "SKILL.md");
    const sharedSkillPresent = existsSync(sharedSkillPath);

    // Auto-educate every discovered workspace on each heartbeat. This is
    // safe and cheap: writeEducationFiles uses versioned fence markers and
    // is a no-op when each workspace already carries the current-version
    // block. The pre-A1 filter ("only workspaces whose TOOLS.md does not
    // already mention memclaw") would skip workspaces with stale-version
    // content — exactly the population we now need to migrate.
    try {
      const filesResult = writeEducationFiles(
        buildToolsMd(),
        buildAgentsMd(),
      );
      if (filesResult.toolsUpdated > 0 || filesResult.agentsUpdated > 0) {
        console.log(
          `[memclaw] Auto-educated workspaces on heartbeat ` +
            `(TOOLS.md: ${filesResult.toolsUpdated}, AGENTS.md: ${filesResult.agentsUpdated})`,
        );
        // Re-check tools_md presence so setup_status reflects the write.
        for (const wsDir of Object.keys(workspaceFiles)) {
          const wsPath = join(ocBase, wsDir);
          workspaceFiles[wsDir].tools_md =
            existsSync(join(wsPath, "TOOLS.md")) &&
            readFileSync(join(wsPath, "TOOLS.md"), "utf-8").toLowerCase().includes("memclaw");
        }
      }
    } catch (e: unknown) {
      logError("Auto-educate failed", e);
    }

    // Backend reachability, populated by the 10-tick health probe below and
    // by `trackReachability`-wrapped ops in the runtime path.
    const reach = getReachability();

    // Single source of truth for "fully configured" — see
    // plugin/src/config.ts::isMemclawFullyConfigured. Checks that memclaw is
    // allowlisted, enabled, on the load path, and holds the exclusive memory
    // slot. `backend_reachable` below surfaces runtime health separately.
    setupStatus = {
      plugin_loaded: true,
      tools_registered: MEMCLAW_TOOLS.length,
      tools_allowed: toolsAllowed,
      fully_configured: config ? isMemclawFullyConfigured(config) : false,
      agents_educated: educated,
      shared_skill_present: sharedSkillPresent,
      backend_reachable: reach.state,
      backend_reachable_reason: reach.reason,
      backend_reachable_last_check_ms: reach.lastCheckMs || null,
      workspace_files: workspaceFiles,
    };
  } catch {
    // setup_status build failed
  }

  // Deploy-cooldown surfaced to the backend so its auto-upgrade trigger
  // (CAURA-444 commit #4) can skip queueing during the cooldown window.
  // Always reflects the latest cooldown file; reset on successful verify.
  const cooldown = readDeployCooldown();
  const deploy_blocked_until = cooldown.blocked_until || undefined;

  const body: Record<string, unknown> = {
    tenant_id: MEMCLAW_TENANT_ID,
    node_name: MEMCLAW_NODE_NAME,
    fleet_id: MEMCLAW_FLEET_ID || undefined,
    hostname: hostname(),
    ip: ipAddress,
    openclaw_version: openclawVersion,
    os_info: `${platform()} ${release()}`,
    plugin_version: PLUGIN_VERSION,
    plugin_hash: pluginHash,
    install_id: installId,
    agents,
    tools: MEMCLAW_TOOLS,
    metadata: setupStatus ? { setup_status: setupStatus } : undefined,
    // CAURA-444: rolling counters reset on plugin restart. Backend
    // stores latest snapshot per node; aggregated via the existing
    // /fleet/nodes endpoint.
    recall_metrics: getRecallMetrics(),
    // Cooldown signal: when set, the backend's auto-upgrade trigger
    // refuses to queue further deploy commands until this timestamp.
    deploy_blocked_until,
  };

  try {
    const result = (await apiCall("POST", "/fleet/heartbeat", body)) as Record<string, any>;
    if (result?.commands?.length) {
      for (const cmd of result.commands) {
        await processCommand(cmd);
      }
    }
  } catch (e: unknown) {
    logError("heartbeat failed", e);
  }

  // Periodic health check every 10 heartbeats.
  //
  // Feeds the reachability tracker (plugin/src/health.ts). Previously the
  // outcome was only console.warn'd on an empty-index result; now success
  // flips the tracker to "reachable" and network-class failure flips it to
  // "unreachable" so the memory-runtime paths can surface honest
  // availability via `getMemorySearchManager` / `status` / the probes.
  //
  // An empty-results search against a populated tenant can happen for
  // benign reasons (new tenant, throttled embeddings) and is NOT treated as
  // unreachable — only genuine network-class throws are.
  heartbeatCount++;
  if (heartbeatCount % 10 === 0 && MEMCLAW_TENANT_ID) {
    try {
      (await apiCall("POST", "/search", {
        tenant_id: MEMCLAW_TENANT_ID,
        query: "health check",
        top_k: 1,
      })) as Record<string, any>;
      markReachable();
    } catch (e: unknown) {
      const msg = logError("heartbeat health check failed", e);
      markUnreachable(msg || "heartbeat health probe failed");
    }
  }
}

async function processCommand(cmd: {
  id: string;
  command: string;
  payload?: Record<string, unknown>;
  timestamp?: string;
  signature?: string;
}): Promise<void> {
  // Verify command signature (HMAC-SHA256)
  const sigResult = verifyCommandSignature(
    cmd,
    MEMCLAW_API_KEY,
    MEMCLAW_REQUIRE_SIGNED_COMMANDS,
  );
  if (!sigResult.valid) {
    console.warn(
      `[memclaw] Rejected command ${cmd.command} (${cmd.id}): ${sigResult.reason}`,
    );
    // Still report rejection to server (encodeURIComponent is sufficient for URL safety)
    try {
      await apiCall("POST", `/fleet/commands/${encodeURIComponent(cmd.id)}/result`, {
        status: "rejected",
        result: { error: `Signature verification failed: ${sigResult.reason}` },
      });
    } catch {
      // Report failed
    }
    return;
  }

  // Validate cmd.id upfront — before any side effects
  try {
    assertSafePathSegment(cmd.id, "cmd.id");
  } catch (e: unknown) {
    const msg = logError(`Rejected command ${cmd.command}: invalid cmd.id`, e);
    return;
  }

  let status = "done";
  let result: Record<string, unknown> = {};
  // CAURA-000: set inside the deploy/restart success branches; the
  // restart MUST be scheduled AFTER the result POST resolves so a
  // slow POST isn't killed mid-flight by the systemctl SIGTERM.
  // Customer prod-data: 1,381 commands stuck at ``acked`` because of
  // exactly this race — the pre-fix code scheduled
  // ``setTimeout(systemctl restart, 2000)`` BEFORE awaiting the POST,
  // so the POST + SIGTERM raced and the backend never saw "done".
  let shouldRestart = false;

  try {
    if (cmd.command === "deploy" || cmd.command === "update_plugin") {
      const payload = cmd.payload || {};
      const source = payload.source as string | undefined;
      const env_vars = payload.env_vars as Record<string, string> | undefined;
      // Backend stamps `target_version` in the deploy payload (see
      // core_api/routes/fleet.py:_maybe_queue_auto_upgrade). We use it
      // as the cooldown key when /plugin-manifest is unreachable so a
      // repeated failed deploy of the same target still engages the
      // local cooldown machinery on the next attempt.
      const targetVersion = (payload.target_version as string | undefined) ?? undefined;
      let sourceCode = source;

      if (!sourceCode && MEMCLAW_API_URL) {
        // Fetch the canonical file list from /plugin-manifest. Falls
        // back to a built-in default array when the backend doesn't
        // expose the endpoint yet (back-compat with pre-CAURA-444
        // backends). The default list MUST stay aligned with the
        // backend's _plugin_files for OLD plugin->NEW backend; this
        // is asserted by the Python test_plugin_source_manifest tests.
        const FALLBACK_SRC_FILES = [
          "index.ts", "prompt-section.ts", "tools.ts", "tool-specs.ts",
          "version.ts", "env.ts", "transport.ts", "validation.ts",
          "config.ts", "paths.ts", "logger.ts", "resolve-agent.ts",
          "tool-definitions.ts", "deploy.ts", "heartbeat.ts",
          "educate.ts", "context-engine.ts", "agent-auth.ts",
          "health.ts", "install-id.ts", "identity.ts",
          "reconcile-skills.ts",
          // ``keystones.ts`` MUST be in this list. ``context-engine.ts``
          // statically imports ``"./keystones.js"``. When
          // ``/plugin-manifest`` is unreachable (older backend or
          // network blip) and we fall back to this hardcoded array,
          // the 404-non-fatal path only helps files already in the
          // list — a name we forgot is silently NEVER fetched. On a
          // fresh-ish install where ``keystones.ts`` doesn't yet exist
          // on disk, ``npx tsc`` fails with TS2307, cooldown engages,
          // and the upgrade loops without progress. Lockstep with
          // ``_plugin_files`` in ``core_api/routes/plugin.py``.
          "keystones.ts",
        ];
        const FALLBACK_ROOT_FILES = [
          "openclaw.plugin.json", "tools.json", "skills/memclaw/SKILL.md",
        ];

        let srcFiles: string[] = FALLBACK_SRC_FILES;
        let rootFiles: string[] = FALLBACK_ROOT_FILES;
        let manifestVersion: string | undefined;
        try {
          const mUrl = new URL(
            `${MEMCLAW_API_PREFIX}/plugin-manifest`,
            MEMCLAW_API_URL,
          ).toString();
          // ``X-API-Key`` is required for the enterprise gateway's auth
          // subrequest on ``/api/v1/*``. Without it, the manifest fetch
          // 401s in production behind nginx and the deploy silently
          // falls back to ``FALLBACK_SRC_FILES`` — defeating the whole
          // point of having a manifest endpoint. The endpoint is
          // unauthenticated in core-api itself (see plugin_manifest
          // docstring), so sending the key is just to satisfy the
          // gateway; the bootstrap-router alias path is the
          // unauthenticated route for fresh installs.
          const mHeaders: Record<string, string> = {};
          if (MEMCLAW_API_KEY) mHeaders["X-API-Key"] = MEMCLAW_API_KEY;
          const mRes = await fetch(mUrl, {
            headers: mHeaders,
            signal: AbortSignal.timeout(10_000),
          });
          if (mRes.ok) {
            const m = await mRes.json() as {
              version?: string;
              src_files?: string[];
              root_files?: string[];
            };
            if (Array.isArray(m.src_files) && m.src_files.length > 0) {
              srcFiles = m.src_files;
            }
            if (Array.isArray(m.root_files)) {
              rootFiles = m.root_files;
            }
            if (typeof m.version === "string" && m.version) {
              manifestVersion = m.version;
            }
            // SECURITY: ``manifestVersion`` is later interpolated into a
            // TypeScript source file (``version.ts``) and a JSON-like
            // package.json bump. A value containing ``"`` or ``\n`` (or
            // arbitrary unicode) would produce syntactically invalid
            // TypeScript that fails to compile — or worse, content that
            // executes during build. Restrict to a strict semver-ish
            // charset (alphanumerics, ``.``, ``-``, ``+``, ``_``); on
            // mismatch, treat as no version (drops back to the
            // payload.target_version fallback for cooldown bookkeeping).
            if (manifestVersion && !/^[\w.\-+]+$/.test(manifestVersion)) {
              console.warn(
                `[memclaw] manifest version "${manifestVersion}" contains unexpected characters — ignoring`,
              );
              manifestVersion = undefined;
            }
            console.log(
              `[memclaw] manifest fetched: target=v${manifestVersion || "?"} ` +
                `src=${srcFiles.length} root=${rootFiles.length}`,
            );
          } else if (mRes.status !== 404) {
            console.warn(
              `[memclaw] /plugin-manifest returned ${mRes.status}; using fallback file list`,
            );
          }
        } catch (e: unknown) {
          console.warn(
            `[memclaw] /plugin-manifest fetch failed (back-compat fallback): ${(e as Error).message}`,
          );
        }

        // Effective version for cooldown bookkeeping. Prefer the live
        // manifest version (most authoritative); fall back to the
        // backend-stamped `payload.target_version`. Without this
        // fallback, a failed deploy against an older backend (manifest
        // 404) would silently bypass the cooldown gate and re-attempt
        // the same broken deploy on every heartbeat.
        const effectiveVersion: string | undefined =
          manifestVersion ?? targetVersion;

        // Cooldown gate — refuse deploys for a previously-failed version.
        if (effectiveVersion) {
          const blocked = isDeployBlocked(effectiveVersion);
          if (blocked.blocked) {
            status = "failed";
            result = {
              error: "deploy blocked by cooldown",
              failed_version: effectiveVersion,
              blocked_until: blocked.until,
            };
            // Skip the rest of this branch.
            srcFiles = [];
            rootFiles = [];
          }
        }

        // SECURITY: validate manifest-provided filenames BEFORE any
        // disk write. Server-supplied entries from /plugin-manifest
        // (or the fallback list — but we control that one) flow into
        // ``join(pluginDir, relPath)`` further down. A name like
        // ``../../etc/passwd`` or ``/etc/passwd`` would escape
        // ``pluginDir`` because ``path.join`` happily resolves ``..``
        // segments. Reject:
        //   - any segment that is ``..``, ``.``, or empty (``a//b``)
        //   - any name with a NUL byte (some filesystems truncate at \0)
        //   - any name with a leading ``/`` (absolute path)
        // Failing one entry aborts the whole deploy — partial writes
        // are worse than no writes (build would compile a mix of new
        // + old files).
        for (const name of [...srcFiles, ...rootFiles]) {
          const parts = name.split("/");
          if (
            parts.some((p) => p === ".." || p === "." || p === "") ||
            name.startsWith("/") ||
            name.includes("\0")
          ) {
            console.error(
              `[memclaw] deploy aborted: unsafe filename in manifest: ${name}`,
            );
            status = "failed";
            result = {
              error: "Manifest contained unsafe filename — deploy aborted",
            };
            srcFiles = [];
            rootFiles = [];
            break;
          }
        }

        const pluginDir = getPluginDir();
        const srcDir = join(pluginDir, "src");

        if (srcFiles.length > 0) {
          // Snapshot existing files for rollback on failure (src + root)
          const backups = new Map<string, string>();
          for (const f of srcFiles) {
            const p = join(srcDir, f);
            if (existsSync(p)) backups.set("src/" + f, readFileSync(p, "utf-8"));
          }
          for (const f of rootFiles) {
            const p = join(pluginDir, f);
            if (existsSync(p)) backups.set(f, readFileSync(p, "utf-8"));
          }

          // Fetch all files into memory first — don't touch disk until all succeed.
          //
          // Back-compat note: 404 is treated as NON-FATAL. The plugin's
          // fallback srcFiles list mirrors current main; older backends
          // (built before some file was added to their `_plugin_files`
          // allowlist) will 404 on those specific names. The local copy
          // from the previous install is still on disk and the build can
          // proceed without an update for that file. Only NON-404 errors
          // (network failures, 500s, empty/oversize bodies) make the
          // overall fetch fail.
          const fetched = new Map<string, string>();
          let fetchOk = true;
          const skipped404: string[] = [];
          const allFiles: Array<{ name: string; isRoot: boolean }> = [
            ...srcFiles.map((f) => ({ name: f, isRoot: false })),
            ...rootFiles.map((f) => ({ name: f, isRoot: true })),
          ];
          for (const { name, isRoot } of allFiles) {
            const fetchController = new AbortController();
            const fetchTimeout = setTimeout(() => fetchController.abort(), 30_000);
            try {
              const url = new URL(
                `${MEMCLAW_API_PREFIX}/plugin-source?file=${encodeURIComponent(name)}`,
                MEMCLAW_API_URL,
              ).toString();
              const res = await fetch(url, { signal: fetchController.signal });
              if (res.ok) {
                const text = await res.text();
                if (text.length > 0 && text.length <= MAX_SOURCE_SIZE) {
                  fetched.set((isRoot ? "" : "src/") + name, text);
                } else {
                  fetchOk = false;
                  if (text.length > MAX_SOURCE_SIZE) {
                    console.warn(`[memclaw] Fetched file ${name} exceeds MAX_SOURCE_SIZE`);
                  } else {
                    console.warn(`[memclaw] Fetched file ${name} returned empty body`);
                  }
                }
              } else if (res.status === 404) {
                // Backend doesn't expose this file — older than the
                // plugin's fallback list. Keep the local copy.
                skipped404.push(name);
              } else {
                fetchOk = false;
                console.warn(`[memclaw] Fetched file ${name} returned HTTP ${res.status}`);
              }
            } catch (e: unknown) {
              fetchOk = false;
              console.warn(
                `[memclaw] Fetched file ${name} threw: ${(e as Error).message}`,
              );
            } finally {
              clearTimeout(fetchTimeout);
            }
          }
          if (skipped404.length > 0) {
            console.warn(
              `[memclaw] /plugin-source 404 on ${skipped404.length} files (older backend?), ` +
                `keeping local copies: ${skipped404.join(", ")}`,
            );
          }
          if (fetchOk) {
            try {
              // Write all fetched files to disk (creating subdirs as needed)
              for (const [relPath, text] of fetched) {
                const target = join(pluginDir, relPath);
                const dir = target.substring(0, target.lastIndexOf("/"));
                if (dir && !existsSync(dir)) mkdirSync(dir, { recursive: true });
                writeFileSync(target, text, "utf-8");
              }
              console.log(`[memclaw] deploy: wrote ${fetched.size} files`);
              // Stamp version.ts from the manifest's version BEFORE
              // building. This is the fix for drift 2 — the prebuild
              // step references a monorepo path that doesn't exist on
              // a flat install, so without this stamp version.ts
              // retains its prior value and the new build reports the
              // OLD plugin_version after restart.
              // version.ts / package.json stamping requires a known-good
              // canonical version — only the live manifest can provide it.
              // The backend-stamped `payload.target_version` is a hint
              // for cooldown bookkeeping, NOT a substitute for the
              // server's authoritative manifest.version (which the
              // post-restart verifier compares against). So this block
              // stays keyed on `manifestVersion`.
              if (manifestVersion) {
                const versionTs =
                  "// Auto-generated by deploy command from /plugin-manifest — do not edit\n" +
                  `export const PLUGIN_VERSION = "${manifestVersion}";\n`;
                writeFileSync(join(srcDir, "version.ts"), versionTs, "utf-8");
                // Also update package.json to keep `npm view` honest.
                try {
                  const pkgPath = join(pluginDir, "package.json");
                  if (existsSync(pkgPath)) {
                    const pkg = JSON.parse(readFileSync(pkgPath, "utf-8"));
                    pkg.version = manifestVersion;
                    writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf-8");
                  }
                } catch (e: unknown) {
                  logError("Failed to update package.json version", e);
                }
              }
              // Pending marker uses `effectiveVersion` so cooldown
              // can engage on the manifest-404 path too.
              if (effectiveVersion) {
                writeDeployPending(effectiveVersion);
              }
              // Build with `npx tsc` directly, NOT `npm run build`.
              // The latter triggers package.json's `prebuild` hook which
              // calls `bash ../scripts/gen-version.sh` — a monorepo path
              // that doesn't exist on a flat install. We already stamp
              // version.ts directly above when manifestVersion is set,
              // so the prebuild step is redundant AND fatal here. Going
              // straight through tsc keeps the build hermetic.
              console.log(`[memclaw] deploy: invoking npx tsc (timeout=${BUILD_TIMEOUT_MS}ms)`);
              const buildOutput = execSync("npx tsc 2>&1", {
                cwd: pluginDir,
                encoding: "utf-8",
                timeout: BUILD_TIMEOUT_MS,
              });
              console.log(`[memclaw] deploy: build succeeded, restart will be scheduled after result POST`);
              result = {
                ok: true,
                // Report the version that the cooldown / verifier path
                // is keyed on so operators see the same value in the
                // command result + nodes.metadata.
                target_version: effectiveVersion,
                buildOutput: buildOutput.slice(-2000),
                restarting: true,
              };
              shouldRestart = true;
            } catch (e: unknown) {
              const errMsg = e instanceof Error ? e.message : String(e);
              console.warn(`[memclaw] deploy: build failed — ${errMsg.slice(0, 200)}`);
              // Write or build failed — restore backups (both src + root)
              for (const [relPath, content] of backups) {
                try {
                  writeFileSync(join(pluginDir, relPath), content, "utf-8");
                } catch {
                  // Restore failed for this file
                }
              }
              console.log(`[memclaw] deploy: backups restored (${backups.size} files), status=failed`);
              // Cooldown the failed version so the backend stops
              // re-queuing for it. Uses `effectiveVersion` so a failed
              // deploy from a `target_version`-stamped payload (backend
              // auto-upgrade trigger) is correctly blocked even when
              // /plugin-manifest is unreachable.
              if (effectiveVersion) {
                writeDeployCooldown(effectiveVersion, "build-failed");
              }
              clearDeployPending();
              status = "failed";
              const err = e as Error & { stdout?: string; stderr?: string };
              result = {
                error: "Deploy failed: " + (err.message || ""),
                buildOutput: (err.stdout || err.stderr || "").slice(-2000),
              };
            }
          } else {
            status = "failed";
            result = { error: "Failed to fetch plugin source files" };
          }
        }
      } else if (sourceCode) {
        const deployResult = await deployPlugin(sourceCode, env_vars);
        if (deployResult.ok) {
          result = { ok: true, buildOutput: (deployResult.buildOutput || "").slice(-2000), restarting: true };
          shouldRestart = true;
        } else {
          status = "failed";
          result = { error: deployResult.error, buildOutput: deployResult.buildOutput };
        }
      } else {
        status = "failed";
        result = { error: "no source provided" };
      }
    } else if (cmd.command === "educate") {
      const payload = cmd.payload || {};
      const prompt = payload.prompt as string | undefined;
      const agent_ids = payload.agent_ids as string[] | undefined;
      const force = payload.force === true;
      if (!prompt) {
        status = "failed";
        result = { error: "no prompt" };
      } else {
        assertPromptLength(prompt);
        const educateResult = educateAgents(prompt, agent_ids);
        const filesResult = writeEducationFiles(
          buildToolsMd(),
          buildAgentsMd(),
          agent_ids,
          undefined,
          { force },
        );
        const noEffect =
          educateResult.verified === 0 &&
          filesResult.toolsUpdated === 0 &&
          filesResult.agentsUpdated === 0;
        if (noEffect) {
          status = "failed";
          result = {
            ok: false,
            error:
              educateResult.failed.length > 0
                ? `All writes failed: ${educateResult.failed.map((f: { workspace: string; error: string }) => `${f.workspace}: ${f.error}`).join("; ")}`
                : "No workspace directories found",
            workspaces: 0,
          };
        } else {
          result = {
            ok: true,
            workspaces: educateResult.count,
            verified: educateResult.verified,
            educated: educateResult.educated,
            failed: educateResult.failed.length ? educateResult.failed : undefined,
            files: {
              tools_updated: filesResult.toolsUpdated,
              agents_updated: filesResult.agentsUpdated,
            },
          };
        }
      }
    } else if (cmd.command === "ping") {
      result = {
        ok: true,
        pong: true,
        node_name: MEMCLAW_NODE_NAME,
        plugin_version: PLUGIN_VERSION,
        uptime_ms: Math.floor(process.uptime() * 1000),
        timestamp: new Date().toISOString(),
      };
    } else if (cmd.command === "restart") {
      result = { ok: true, restarting: true };
      shouldRestart = true;
    } else {
      status = "failed";
      result = { error: `Unknown command: ${cmd.command}` };
    }
  } catch (e: unknown) {
    status = "failed";
    const msg = logError(`command ${cmd.command} failed`, e);
    result = { error: msg };
  }

  // Report result — cmd.id already validated at function entry.
  // We log the POST attempt + outcome so a stuck "acked" command in
  // the backend is immediately attributable (plugin didn't POST vs
  // backend ack'd-but-didn't-update). This saved an hour of wet-test
  // debugging on the CAURA-444 v2.3.0->v2.4.0 transition path.
  try {
    await apiCall("POST", `/fleet/commands/${encodeURIComponent(cmd.id)}/result`, {
      status,
      result,
    });
    console.log(`[memclaw] command ${cmd.command} reported: status=${status}`);
  } catch (re: unknown) {
    console.warn(
      `[memclaw] command ${cmd.command} result POST failed: ${(re as Error).message}`,
    );
  }

  // CAURA-000: schedule the gateway restart AFTER the result POST
  // resolves. Pre-fix this was three duplicated ``setTimeout`` blocks
  // scheduled INSIDE the deploy/restart branches above (before the
  // POST), so a slow POST got killed mid-flight by the systemctl
  // SIGTERM and the backend never saw "done" — every node that hit
  // this race accumulated stuck-``acked`` deploy commands at 1/heartbeat
  // (customer prod: 1,381 across the fleet, 1,223 on one node, the
  // exact source of the "SIGTERM every 60s" cycle the customer
  // reported). The restart still happens whether the POST succeeded
  // or failed — the deploy/restart command itself completed; the POST
  // is only the bookkeeping channel to the backend.
  if (shouldRestart) {
    _scheduleGracefulRestart();
  }
}
