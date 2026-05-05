/**
 * Heartbeat loop and command processing.
 *
 * Security fixes:
 * - HMAC signature verification on commands before execution
 * - Safe ID validation on cmd.id before URL interpolation
 * - Prompt length cap on educate commands
 * - Workspace path stripping from telemetry (hash instead)
 */

import { readFileSync, writeFileSync, existsSync, readdirSync, unlinkSync } from "fs";
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
import {
  verifyCommandSignature,
  assertSafePathSegment,
  assertPromptLength,
} from "./validation.js";
import { logError } from "./logger.js";
import { getInstallId } from "./install-id.js";
import { getDisplayName } from "./identity.js";

let heartbeatCount = 0;
let bakCleanupDone = false;

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
  if (!MEMCLAW_TENANT_ID || !MEMCLAW_NODE_NAME) return;

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

  try {
    if (cmd.command === "deploy" || cmd.command === "update_plugin") {
      const payload = cmd.payload || {};
      const source = payload.source as string | undefined;
      const env_vars = payload.env_vars as Record<string, string> | undefined;
      let sourceCode = source;

      if (!sourceCode && MEMCLAW_API_URL) {
        const srcFiles = [
          "index.ts", "prompt-section.ts", "tools.ts", "version.ts",
          "env.ts", "transport.ts", "validation.ts", "config.ts",
          "resolve-agent.ts", "tool-definitions.ts", "deploy.ts",
          "heartbeat.ts", "educate.ts", "context-engine.ts", "health.ts",
        ];
        const pluginDir = getPluginDir();
        const srcDir = join(pluginDir, "src");

        // Snapshot existing files for rollback on failure
        const backups = new Map<string, string>();
        for (const f of srcFiles) {
          const p = join(srcDir, f);
          if (existsSync(p)) backups.set(f, readFileSync(p, "utf-8"));
        }

        // Fetch all files into memory first — don't touch disk until all succeed
        const fetched = new Map<string, string>();
        let fetchOk = true;
        for (const f of srcFiles) {
          const fetchController = new AbortController();
          const fetchTimeout = setTimeout(() => fetchController.abort(), 30_000);
          try {
            const url = new URL(
              `${MEMCLAW_API_PREFIX}/plugin-source?file=${encodeURIComponent(f)}`,
              MEMCLAW_API_URL,
            ).toString();
            const res = await fetch(url, { signal: fetchController.signal });
            if (res.ok) {
              const text = await res.text();
              if (text.length > 0 && text.length <= MAX_SOURCE_SIZE) {
                fetched.set(f, text);
              } else {
                fetchOk = false;
                if (text.length > MAX_SOURCE_SIZE) {
                  console.warn(`[memclaw] Fetched file ${f} exceeds MAX_SOURCE_SIZE`);
                } else {
                  console.warn(`[memclaw] Fetched file ${f} returned empty body`);
                }
              }
            } else {
              fetchOk = false;
            }
          } catch {
            fetchOk = false;
          } finally {
            clearTimeout(fetchTimeout);
          }
        }
        if (fetchOk) {
          try {
            // Write all fetched files to disk
            for (const [f, text] of fetched) {
              writeFileSync(join(srcDir, f), text, "utf-8");
            }
            const buildOutput = execSync("npm run build 2>&1", {
              cwd: pluginDir,
              encoding: "utf-8",
              timeout: BUILD_TIMEOUT_MS,
            });
            result = { ok: true, buildOutput: buildOutput.slice(-2000), restarting: true };
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
          } catch (e: unknown) {
            // Write or build failed — restore backups
            for (const [f, content] of backups) {
              try {
                writeFileSync(join(srcDir, f), content, "utf-8");
              } catch {
                // Restore failed for this file
              }
            }
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
      } else if (sourceCode) {
        const deployResult = await deployPlugin(sourceCode, env_vars);
        if (deployResult.ok) {
          result = { ok: true, buildOutput: (deployResult.buildOutput || "").slice(-2000), restarting: true };
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
    } else {
      status = "failed";
      result = { error: `Unknown command: ${cmd.command}` };
    }
  } catch (e: unknown) {
    status = "failed";
    const msg = logError(`command ${cmd.command} failed`, e);
    result = { error: msg };
  }

  // Report result — cmd.id already validated at function entry
  try {
    await apiCall("POST", `/fleet/commands/${encodeURIComponent(cmd.id)}/result`, {
      status,
      result,
    });
  } catch {
    // Report failed
  }
}
