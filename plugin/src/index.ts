/**
 * MemClaw OpenClaw Plugin — registration glue.
 *
 * Registers MemClaw tools (set + order derived from plugin/tools.json
 * via MEMCLAW_TOOLS), gateway methods, prompt section, memory runtime,
 * context engine, and heartbeat loop.
 *
 * All implementation is in separate modules for maintainability:
 * - env.ts          — environment config, tenant resolution, constants
 * - transport.ts    — HTTP transport with default timeout
 * - validation.ts   — UUID, HTTPS, path, HMAC, prompt-length checks
 * - config.ts       — OpenClaw config helpers, auto-fix allowlist
 * - tool-specs.ts   — typed loader for plugin/tools.json (SoT)
 * - tool-definitions.ts — createToolFromSpec factory + endpoint dispatch
 * - deploy.ts       — plugin deployment logic
 * - heartbeat.ts    — heartbeat loop + command processing
 * - educate.ts      — agent education (HEARTBEAT.md writes)
 * - context-engine.ts — MemClawContextEngine lifecycle
 * - resolve-agent.ts  — agent identity resolution
 */

import { readFileSync, writeFileSync, existsSync, statSync } from "fs";
import { join } from "path";
import { createHash } from "crypto";
import { getOpenClawBaseDir, getPluginEnvPath } from "./paths.js";

import {
  MEMCLAW_API_URL,
  MEMCLAW_API_KEY,
  MEMCLAW_FLEET_ID,
  MEMCLAW_TENANT_ID,
  MEMCLAW_NODE_NAME,
  ensureTenantId,
  fetchToolDescriptions,
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_INITIAL_DELAY_MS,
  MAX_SOURCE_SIZE,
} from "./env.js";
import { apiCall, parseSearchItems } from "./transport.js";
import { PLUGIN_VERSION } from "./version.js";
import {
  memclawPromptSectionBuilder,
  memclawPromptSectionText,
} from "./prompt-section.js";
import { MEMCLAW_TOOLS } from "./tools.js";
import {
  autoFixAllowlist,
  readOpenClawConfig,
  getOpenClawConfigPath,
  getPluginDir,
  getPluginSrcPath,
  getMissingTools,
  isMemclawAllowed,
  isMemclawEnabled,
  isMemclawPathLoaded,
  isMemclawFullyConfigured,
  isContextEngineSlotClaimed,
  shouldRunAutoFix,
} from "./config.js";
import { createToolFromSpec } from "./tool-definitions.js";
import { deployPlugin } from "./deploy.js";
import { sendHeartbeat } from "./heartbeat.js";
import {
  educateAgents,
  writeEducationFiles,
  buildToolsMd,
  buildAgentsMd,
} from "./educate.js";
import { MemClawContextEngine } from "./context-engine.js";
import {
  getReachability,
  markReachable,
  markUnreachable,
  trackReachability,
} from "./health.js";
import { logError, logErrorCritical } from "./logger.js";

// Re-export for test compatibility
export { educateAgents, writeEducationFiles } from "./educate.js";

// --- Shared search helper (used by registerMemoryRuntime + getMemorySearchManager) ---

async function searchMemories(
  query: string,
  limit: number,
): Promise<
  Array<{
    content: string;
    path: string;
    score: number;
    metadata: Record<string, unknown>;
  }>
> {
  // trackReachability updates the health tracker on success and on
  // network-class failure. HTTP 4xx "no" answers are not network-class and
  // leave the tracker untouched — the backend is fine, the query just had
  // no match / bad scope.
  return trackReachability(async () => {
    const tid = await ensureTenantId();
    const results = (await apiCall(
      "POST",
      "/search",
      { tenant_id: tid, query, top_k: limit },
    )) as Record<string, unknown> | Record<string, unknown>[];
    const arr = parseSearchItems(results);
    return arr.map((m: Record<string, unknown>) => ({
      content: (m.content as string) || "",
      path: `memclaw://${m.id}`,
      score: (m.score as number) ?? (m.similarity as number) ?? 0,
      metadata: {
        memory_type: m.memory_type,
        agent_id: m.agent_id,
        created_at: m.created_at,
        title: m.title,
      },
    }));
  });
}

// --- OpenClaw plugin registration ---

// OpenClaw invokes register() once per plugin slot the plugin fills
// (regular plugin slot + memory-runtime slot). Guard process-wide
// side effects (tenant resolution, heartbeat, tool-description fetch)
// so they don't run twice and produce duplicated logs.
let sideEffectsBootstrapped = false;

const memclawPlugin = {
  id: "memclaw",
  name: "MemClaw",
  description:
    "Central persistent memory for OpenClaw agents with cross-fleet, multi-agent shared recall",
  configSchema: {
    type: "object" as const,
    properties: {},
    required: [] as string[],
  },

  register(api: Record<string, any>) {
    // Resolve tenant_id (with retry) and use result to gate heartbeat loop.
    // Route through ensureTenantId() so the _tenantPromise cache is populated —
    // without this, a concurrent ensureTenantId() caller (context-engine
    // bootstrap, first tool invocation) would spawn a second independent
    // resolution loop and duplicate every log line.
    if (!sideEffectsBootstrapped) {
      sideEffectsBootstrapped = true;
      ensureTenantId()
        .then((tid) => {
          if (tid && MEMCLAW_NODE_NAME) {
            setTimeout(() => {
              sendHeartbeat();
              setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS);
            }, HEARTBEAT_INITIAL_DELAY_MS);
          } else if (tid && !MEMCLAW_NODE_NAME) {
            console.warn("[memclaw] Heartbeat disabled — MEMCLAW_NODE_NAME not set.");
          }
        })
        .catch(() => {
          // ensureTenantId throws when tenant_id can't be resolved.
          // The resolver itself already logged the reason (HTTP error,
          // network error, or missing field), so just emit the
          // user-facing next-step here.
          console.warn(
            "[memclaw] Heartbeat disabled — tenant_id could not be resolved. Set MEMCLAW_TENANT_ID in .env.",
          );
        });
      fetchToolDescriptions()
        .then(() => markReachable())
        .catch((e: unknown) => {
          // fetchToolDescriptions itself swallows errors internally (by
          // design — missing tool descriptions aren't fatal). This catch
          // handles any residual throw; treat as unknown rather than
          // unreachable since the request may have been blocked by a
          // config issue rather than a network failure.
          logError("boot probe fetchToolDescriptions failed", e);
        });
    }

    // Register tools from the SoT (plugin/tools.json), in the order
    // declared by MEMCLAW_TOOLS. The factory pulls name/description from
    // tools.json; drift between the two sets throws at import time.
    for (const name of MEMCLAW_TOOLS) {
      api.registerTool(createToolFromSpec(name), { names: [name] });
    }

    // --- Gateway methods ---

    api.registerGatewayMethod("memclaw.status", ({ respond }: any) => {
      const config = readOpenClawConfig();
      respond({
        id: "memclaw",
        name: "MemClaw",
        version: PLUGIN_VERSION,
        status: "loaded",
        description: memclawPlugin.description,
        apiUrl: MEMCLAW_API_URL,
        fleetId: MEMCLAW_FLEET_ID || null,
        apiKeyHint: MEMCLAW_API_KEY ? MEMCLAW_API_KEY.slice(0, 6) + "..." : null,
        tools: MEMCLAW_TOOLS,
        allowlisted: config ? isMemclawAllowed(config as any) : null,
        enabled: config ? isMemclawEnabled(config as any) : null,
        pathLoaded: config ? isMemclawPathLoaded(config as any) : null,
        fullyConfigured: config ? isMemclawFullyConfigured(config as any) : null,
        toolsAllowed: config
          ? MEMCLAW_TOOLS.every(
              (t) =>
                Array.isArray((config as any)?.tools?.alsoAllow) &&
                (config as any).tools.alsoAllow.includes(t),
            )
          : null,
      });
    });

    api.registerGatewayMethod("memclaw.allowlist.check", ({ respond }: any) => {
      const config = readOpenClawConfig();
      if (!config) {
        respond({ ok: false, error: "openclaw.json not found", path: getOpenClawConfigPath() });
        return;
      }
      const allow = (config as any)?.plugins?.allow;
      respond({
        ok: true,
        allowed: isMemclawAllowed(config as any),
        enabled: isMemclawEnabled(config as any),
        pathLoaded: isMemclawPathLoaded(config as any),
        fullyConfigured: isMemclawFullyConfigured(config as any),
        allowList: Array.isArray(allow) ? allow : [],
        path: getOpenClawConfigPath(),
      });
    });

    api.registerGatewayMethod("memclaw.allowlist.fix", ({ respond }: any) => {
      const { changed, changes, error } = autoFixAllowlist({ forceSlotOverride: true });
      if (error) {
        respond({ ok: false, error: "Failed to write config: " + error });
        return;
      }
      if (!changed) {
        respond({ ok: true, changed: false, message: "Already configured" });
        return;
      }
      respond({
        ok: true,
        changed: true,
        message: "Updated " + changes.join(", ") + ". Restart OpenClaw to apply.",
      });
    });

    // Deploy plugin — receive new source, env vars, build.
    // Gateway methods are already gated by OpenClaw operator auth, but we add
    // a token check as defense-in-depth (consistent with heartbeat HMAC model).
    api.registerGatewayMethod("memclaw.deploy", ({ respond, params }: any) => {
      if (MEMCLAW_API_KEY && params?.token !== MEMCLAW_API_KEY) {
        respond({ ok: false, error: "invalid or missing token" });
        return;
      }
      const { source_code, env_vars } = params || {};
      if (!source_code || typeof source_code !== "string") {
        respond({ ok: false, error: "source_code is required" });
        return;
      }
      if (source_code.length > MAX_SOURCE_SIZE) {
        respond({ ok: false, error: "source_code too large (max 500KB)" });
        return;
      }

      deployPlugin(source_code, env_vars).then((deployResult) => {
        if (deployResult.ok) {
          respond({
            ok: true,
            message: "Deploy successful. Restart gateway to load new code.",
            buildOutput: deployResult.buildOutput,
            envUpdated: deployResult.envUpdated,
            sourcePath: getPluginSrcPath(),
          });
        } else {
          respond({ ok: false, error: deployResult.error, buildOutput: deployResult.buildOutput });
        }
      });
    });

    // Read current plugin source and env vars
    api.registerGatewayMethod("memclaw.deploy.status", ({ respond }: any) => {
      const pluginDir = getPluginDir();
      const srcPath = getPluginSrcPath();
      const envPath = getPluginEnvPath();

      const result: Record<string, unknown> = {
        ok: true,
        pluginDir,
        sourceExists: existsSync(srcPath),
        envExists: existsSync(envPath),
        currentEnv: {} as Record<string, string>,
      };

      try {
        if (existsSync(srcPath)) {
          const stat = statSync(srcPath);
          result.sourceSize = stat.size;
          result.sourceModified = stat.mtime.toISOString();
          const src = readFileSync(srcPath, "utf-8");
          result.sourceHash = createHash("sha256").update(src, "utf-8").digest("hex");
        }
      } catch (e: unknown) {
        logError("deploy.status source read failed", e);
      }

      try {
        if (existsSync(envPath)) {
          const content = readFileSync(envPath, "utf-8");
          const env = result.currentEnv as Record<string, string>;
          for (const line of content.split("\n")) {
            const match = line.match(/^(MEMCLAW_\w+)=(.*)$/);
            if (match) {
              const key = match[1];
              env[key] = key.includes("KEY") ? match[2].slice(0, 6) + "..." : match[2];
            }
          }
        }
      } catch (e: unknown) {
        logError("deploy.status env read failed", e);
      }

      respond(result);
    });

    // --- Auto-educate agents on first load ---
    // SKILL.md is discovered by OpenClaw via the `skills` field in
    // `openclaw.plugin.json` (one copy per node, plugin-root-relative).
    // This block writes the per-workspace artifacts: TOOLS.md and
    // AGENTS.md (both fenced + version-tagged for safe re-runs).
    //
    // Pre-C1 we also wrote a 4-sentence DEFAULT_EDUCATION paragraph
    // into HEARTBEAT.md, fully redundant with TOOLS.md/AGENTS.md/
    // SKILL.md and unfenced. That write is dropped; legacy paragraphs
    // left over from prior installs are cleaned up inside
    // writeEducationFiles via cleanupStaleHeartbeatEducation.
    const educatedFlagPath = join(getPluginDir(), ".educated");
    if (!existsSync(educatedFlagPath)) {
      try {
        const filesResult = writeEducationFiles(
          buildToolsMd(),
          buildAgentsMd(),
        );

        // Write the flag on any successful return from writeEducationFiles,
        // regardless of whether any workspace files were actually updated.
        // The flag's purpose is "education has been attempted", not
        // "something changed". Per-workspace errors are silently absorbed
        // inside writeEducationFiles (via logError) and will be retried on
        // the next heartbeat tick — so NOT writing the flag here would
        // cause re-attempts on every plugin load indefinitely.
        writeFileSync(educatedFlagPath, new Date().toISOString(), "utf-8");

        if (filesResult.toolsUpdated > 0 || filesResult.agentsUpdated > 0) {
          console.log(
            `[memclaw] Auto-educated workspaces ` +
              `(TOOLS.md: ${filesResult.toolsUpdated}, ` +
              `AGENTS.md: ${filesResult.agentsUpdated})`,
          );
        }
      } catch (e: unknown) {
        logErrorCritical("Auto-education failed", e);
      }
    }

    // --- Memory prompt section ---
    let promptSectionRegistered = false;

    try {
      if (typeof api.registerMemoryPromptSection === "function") {
        api.registerMemoryPromptSection(memclawPromptSectionBuilder);
        promptSectionRegistered = true;
      }
    } catch (e: unknown) {
      logError("registerMemoryPromptSection unavailable", e);
    }

    if (!promptSectionRegistered) {
      try {
        if (typeof api.on === "function") {
          const fallbackToolSet = new Set(MEMCLAW_TOOLS);
          api.on("before_prompt_build", async (_event: unknown, _ctx: unknown) => {
            const text = memclawPromptSectionText(fallbackToolSet);
            if (!text) return {};
            return { prependSystemContext: text };
          });
          promptSectionRegistered = true;
        }
      } catch (e: unknown) {
        logError("before_prompt_build fallback failed", e);
      }
    }

    // Auto-fix allowlist on first registration — claims memory slot, ensures
    // v1.0 tool names are in tools.alsoAllow, and removes stale pre-v1.0 names.
    // Gated by .allowlist-applied flag file (same pattern as .educated above).
    // Opt-out: MEMCLAW_AUTO_FIX_CONFIG=false skips auto-fix.
    // Force re-run: MEMCLAW_AUTO_FIX_CONFIG=true ignores the flag file.
    const allowlistFlagPath = join(getPluginDir(), ".allowlist-applied");
    const autoFixEnv = process.env.MEMCLAW_AUTO_FIX_CONFIG;
    const flagExists = existsSync(allowlistFlagPath);
    // Read config once to detect drift so auto-fix re-runs even when the
    // one-time flag is already present: a plugin upgrade that ADDS a tool
    // to MEMCLAW_TOOLS (e.g. memclaw_keystones) would otherwise never land
    // in tools.alsoAllow on an existing install, and a later OpenClaw
    // tools.profile (core tools + alsoAllow only) silently strips it.
    // autoFixAllowlist is idempotent (writes only on change), so a clean
    // install with the flag present still no-ops.
    const preFixConfig = readOpenClawConfig() as Record<string, any> | null;
    const shouldAutoFix = shouldRunAutoFix({
      autoFixEnv,
      flagExists,
      missingToolCount: preFixConfig
        ? getMissingTools(preFixConfig).length
        : MEMCLAW_TOOLS.length,
      contextEngineSlotClaimed: preFixConfig
        ? isContextEngineSlotClaimed(preFixConfig)
        : false,
    });

    if (shouldAutoFix) {
      try {
        const { changed, changes, error } = autoFixAllowlist();
        if (error) {
          console.warn(`[memclaw] Auto-fix allowlist failed: ${error}`);
        } else if (changed) {
          console.log(
            `[memclaw] Config auto-fixed: ${changes.join(", ")}. ` +
            `Restart OpenClaw to activate all ${MEMCLAW_TOOLS.length} tools.`,
          );
        }
        writeFileSync(
          allowlistFlagPath,
          JSON.stringify({ appliedAt: new Date().toISOString(), changed, changes: changes || [] }, null, 2),
          "utf-8",
        );
      } catch (e: unknown) {
        logError("Auto-fix allowlist failed", e);
      }
    } else {
      // Flag exists (or opted out) — emit diagnostics. Two independent
      // warning paths:
      //
      //   1. Missing tools in ``tools.alsoAllow`` → agents can't invoke
      //      those tools. Mutes the tool surface.
      //   2. ``plugins.slots.contextEngine !== "memclaw"`` → OpenClaw
      //      falls back to the default "legacy" context engine, so our
      //      ``ContextEngine.assemble()`` is never called and the
      //      ``<keystone_rules>`` block never reaches the system prompt.
      //      Confirmed against OpenClaw 2026.5.4
      //      ``dist/registry-DFFgCbcm.js:241 resolveContextEngine``.
      //
      // Operators on pre-fix installs need this loud signal — the
      // install-script auto-fix only runs once on first boot; existing
      // nodes won't pick up the slot change on a plain gateway restart
      // without explicit re-fix.
      try {
        const config = readOpenClawConfig() as Record<string, any> | null;
        if (config) {
          const missing = getMissingTools(config);
          if (missing.length > 0) {
            console.warn(
              `[memclaw] WARNING: ${missing.length} of ${MEMCLAW_TOOLS.length} tools not in tools.alsoAllow — ` +
              `agents cannot use: ${missing.join(", ")}`,
            );
            console.warn(
              `[memclaw] Fix: run "openclaw gateway memclaw.allowlist.fix" or set MEMCLAW_AUTO_FIX_CONFIG=true`,
            );
          }
          if (!isContextEngineSlotClaimed(config)) {
            const currentCe = config?.plugins?.slots?.contextEngine;
            console.warn(
              `[memclaw] WARNING: plugins.slots.contextEngine is ${currentCe ? `"${currentCe}"` : "unset"} — ` +
              `keystone rules and dynamic recall WILL NOT inject into agent prompts. ` +
              `OpenClaw will fall back to the default "legacy" context engine.`,
            );
            console.warn(
              `[memclaw] Fix: set plugins.slots.contextEngine to "memclaw" in ~/.openclaw/openclaw.json, ` +
              `or run "openclaw gateway memclaw.allowlist.fix" / set MEMCLAW_AUTO_FIX_CONFIG=true`,
            );
          }
        }
      } catch {
        // Config read failed — don't block startup
      }
    }

    // --- Memory flush plan ---
    //
    // OpenClaw's MemoryFlushPlan contract (see memory-state.d.ts)
    // requires SIX fields:
    //
    //   softThresholdTokens, forceFlushTranscriptBytes, reserveTokensFloor,
    //   prompt, systemPrompt, relativePath
    //
    // Pre-fix we returned {instructions, softThresholdTokens} — wrong
    // field name (instructions vs prompt) AND missing the four
    // other fields. relativePath is the load-bearing omission: when
    // compaction crosses softThresholdTokens, OpenClaw's
    // agent-runner.runtime reads activeMemoryFlushPlan.relativePath
    // and passes it to ensureMemoryFlushTargetFile, which throws
    // "Invalid memory flush target path" on any falsy / absolute
    // value. The error only surfaces on long sessions that actually hit
    // the compaction threshold, so it looked intermittent — but the bug
    // is unconditional. Customer report 2026-05-21.
    //
    // relativePath is the workspace-relative scratch file the
    // compaction sub-agent has append-only write access to during the
    // flush turn. MemClaw's server-side persistence is orthogonal — the
    // sub-agent still calls memclaw_write to capture salient
    // context, but it ALSO needs the file to exist because that's the
    // only filesystem write surface OpenClaw exposes to it. Mirror
    // memory-core's memory/YYYY-MM-DD.md layout but namespace under
    // memclaw/ so we don't collide on hosts running both plugins.
    // The outer try here only catches REGISTRATION-time failure. The
    // resolver itself runs later in OpenClaw's agent-runner stack — any
    // throw there propagates up and crashes the flush turn. Pre-fix two
    // input shapes did exactly that:
    //   1. resolver(null) — destructuring null, = {} default
    //      only fires for undefined, so we'd hit
    //      TypeError: Cannot destructure property 'nowMs' of 'null'.
    //   2. resolver({ nowMs: NaN }) — typeof NaN === "number" so
    //      the fallback didn't fire, then new Date(NaN).toISOString()
    //      throws RangeError: Invalid time value.
    // The defensive resolver below: (a) reads nowMs via optional
    // chaining so null/non-object inputs degrade silently, (b) gates
    // with Number.isFinite so NaN / Infinity fall through to
    // Date.now(), (c) wraps the body in its own try/catch so
    // unforeseen failure modes still hand OpenClaw a valid plan (using
    // a same-day fallback path) rather than crashing the flush turn.
    const buildPlan = (dateStamp: string) => ({
      softThresholdTokens: 4000,
      forceFlushTranscriptBytes: 2 * 1024 * 1024,
      reserveTokensFloor: 20000,
      prompt:
        "Before this conversation is compacted, save any important context to MemClaw. " +
        "Call memclaw_write with a summary of: decisions made, tasks completed, bugs found, " +
        "configuration changes, and any commitments or deadlines discovered in this session. " +
        "Include your agent_id, specific names, dates, paths, and outcomes. " +
        "Use memory_type 'episode' and tag with 'pre-compaction'. " +
        "Do NOT reply to the user from this turn.",
      systemPrompt:
        "You are running inside an OpenClaw memory-flush turn. Your only job is to " +
        "persist salient context to MemClaw via memclaw_write before this conversation " +
        "is compacted. Do not call any other tools. Do not produce a user-visible reply.",
      relativePath: `memclaw/flush-${dateStamp}.md`,
    });
    try {
      if (typeof api.registerMemoryFlushPlan === "function") {
        api.registerMemoryFlushPlan(
          (params?: { cfg?: unknown; nowMs?: number } | null) => {
            try {
              const candidate = params?.nowMs;
              // Number.isFinite(-1) === true, so a negative nowMs
              // would slip through and produce a 1969-era relativePath.
              // Add a positive-lower-bound guard so test-time mocks or
              // time-travel scenarios degrade to Date.now() rather
              // than silently writing into pre-epoch-named files.
              const ts =
                typeof candidate === "number" &&
                Number.isFinite(candidate) &&
                candidate > 0
                  ? candidate
                  : Date.now();
              return buildPlan(new Date(ts).toISOString().slice(0, 10));
            } catch (e: unknown) {
              logError("MemoryFlushPlan resolver failed", e);
              return buildPlan(new Date().toISOString().slice(0, 10));
            }
          },
        );
      }
    } catch (e: unknown) {
      logError("registerMemoryFlushPlan failed", e);
    }

    // --- Memory runtime ---
    //
    // The runtime registered here satisfies OpenClaw's `MemoryPluginRuntime`
    // contract from `src/plugins/memory-state.ts`. The two public entry points
    // OpenClaw actually invokes are `resolveMemoryBackendConfig` (at startup)
    // and `getMemorySearchManager` (per memory-op request). Both have typed
    // error channels; using them is the difference between loud failure and
    // silent drop.
    //
    // Failure-surfacing strategy:
    //   - Manager-creation-time failure (unreachable backend, not configured):
    //     return `{manager: null, error: reason}`. OpenClaw's memory-core
    //     caller short-circuits to `buildMemorySearchUnavailableResult(error)`,
    //     which surfaces `{results: [], disabled: true, unavailable: true,
    //     error, warning, action}` to the model.
    //   - Per-call failure (transient network blip during a session):
    //     let the thrown error from `apiCall` propagate. OpenClaw's caller
    //     wraps in try/catch and routes the catch through the same
    //     unavailable-result builder.
    //
    // Neither path silently returns `[]` / `null` anymore.
    try {
      if (typeof api.registerMemoryRuntime === "function") {
        api.registerMemoryRuntime({
          // Required by OpenClaw >=2026.4.x gateway — memory slot plugins must
          // expose this so the gateway can resolve backend config at startup.
          resolveMemoryBackendConfig(_params: Record<string, unknown>) {
            // memoryFlushWritePath is NOT on this contract — that field
            // name was a 2026-04 misunderstanding. The actual flush-time
            // file lives on the MemoryFlushPlan.relativePath returned
            // by registerMemoryFlushPlan above (different code path,
            // different contract). Returning it here was inert but
            // misleading; removed so future readers don't think disabling
            // local-file persistence happens here.
            return { backend: "memclaw" };
          },
          async getMemorySearchManager(_params: Record<string, unknown>) {
            // Refuse to hand back a manager when we already know the backend
            // is unreachable or when required config is missing. The
            // `{manager: null, error}` shape is OpenClaw's typed unreachability
            // channel; the caller surfaces it as a "memory unavailable"
            // result to the model instead of treating empty as success.
            if (!MEMCLAW_API_URL) {
              return {
                manager: null,
                error: "MemClaw plugin unconfigured: MEMCLAW_API_URL not set",
              };
            }
            const health = getReachability();
            if (health.state === "unreachable") {
              // Surface "unavailable" rather than "unreachable" because the
              // tracker's `unreachable` state stores reasons that may not be
              // network-reachability issues — e.g. the anti-stampede path in
              // probeEmbeddingAvailability flips to unreachable on persistent
              // 4xx/auth failures so the fast path short-circuits subsequent
              // probes. Framing those as "unreachable" would misdirect
              // operators toward investigating a network fault when the real
              // problem is auth / config.
              return {
                manager: null,
                error: `MemClaw backend unavailable: ${health.reason ?? "unknown reason"}`,
              };
            }

            const manager = {
              // Per-call failure path: let apiCall errors propagate. OpenClaw
              // wraps this call in try/catch (see
              // openclaw/openclaw:extensions/memory-core/src/tools.ts) and
              // surfaces a structured "unavailable" result to the model.
              async search(query: string, opts?: { limit?: number }) {
                return searchMemories(query, opts?.limit ?? 5);
              },
              // readFile is not a MemClaw concept — memories are fetched by
              // id, not by path. Return an empty MemoryReadResult-shaped
              // value (type-conformant) rather than `null` (type violation
              // that OpenClaw silently coerces).
              async readFile(readParams: Record<string, unknown>) {
                return {
                  text: "",
                  path: String(readParams?.relPath ?? ""),
                  truncated: false,
                };
              },
              // Surface reachability via the typed `fallback` field rather
              // than lying with `status: "configured"`. OpenClaw's
              // MemoryProviderStatus type carries `fallback: {from, reason?}`
              // for exactly this.
              status() {
                const hs = getReachability();
                const base = {
                  provider: "memclaw",
                  backend: "memclaw-api" as const,
                  apiUrl: MEMCLAW_API_URL,
                };
                if (hs.state === "unreachable") {
                  return {
                    ...base,
                    status: "unreachable",
                    fallback: { from: "memclaw-api", reason: hs.reason },
                    lastProbeMs: hs.lastCheckMs,
                  };
                }
                return {
                  ...base,
                  status: hs.state === "reachable" ? "configured" : "configuring",
                  lastProbeMs: hs.lastCheckMs,
                };
              },
              // Honest probes: consult the tracker (populated by heartbeat
              // and by trackReachability-wrapped ops), fall back to "unknown"
              // reported as unavailable rather than lying as available.
              async probeEmbeddingAvailability() {
                const hs = getReachability();
                if (hs.state === "reachable") return { ok: true };
                if (hs.state === "unreachable") {
                  return { ok: false, error: hs.reason ?? "backend unreachable" };
                }
                // state === "unknown": first call after boot before heartbeat
                // probed. Cheap live check by attempting a tiny search; it
                // updates the tracker as a side-effect via trackReachability.
                try {
                  await searchMemories("__memclaw_probe__", 1);
                  return { ok: true };
                } catch (e: unknown) {
                  const msg = String((e as { message?: unknown })?.message ?? e);
                  // trackReachability only flips unreachable on network-class
                  // throws; 4xx/auth failures leave the tracker in "unknown".
                  // Without this markUnreachable, every subsequent probe
                  // would re-issue a live search on persistent non-network
                  // errors — a request-per-call stampede. Advance past
                  // unknown here so the cached answer short-circuits next call.
                  //
                  // AbortError excepted: cancellations from
                  // `AbortController.abort()` (timeouts, lifecycle teardown)
                  // are not evidence that the backend is unhealthy. Marking
                  // unreachable on abort would spuriously flip a healthy
                  // backend and suppress future ops until the heartbeat's
                  // 10-tick probe runs.
                  if ((e as { name?: unknown })?.name !== "AbortError") {
                    markUnreachable(msg);
                  }
                  return { ok: false, error: msg };
                }
              },
              async probeVectorAvailability() {
                // Only an explicit "unreachable" state is a definitive "no";
                // "unknown" (pre-first-probe / transient) should not block
                // vector use, matching getMemorySearchManager's own gating
                // which only refuses on state === "unreachable".
                return getReachability().state !== "unreachable";
              },
              async close() {
                // no-op — MemClaw manages connections server-side
              },
            };
            return { manager, error: null };
          },
          async closeAllMemorySearchManagers() {
            // no-op — MemClaw manages connections server-side
          },
        });
      }
    } catch (e: unknown) {
      logError("registerMemoryRuntime failed", e);
    }

    // --- Context engine ---
    try {
      if (typeof api.registerContextEngine === "function") {
        api.registerContextEngine("memclaw", (config: Record<string, unknown> | undefined | null) => {
          return new MemClawContextEngine(config);
        });
        console.log("[memclaw] ContextEngine 'memclaw' registered");
      }
    } catch (e: unknown) {
      logError("registerContextEngine failed", e);
    }

    // --- Boot diagnostic line (CAURA-000 forensic anchor) ---
    //
    // One-shot startup log so customer reports can paste a single
    // grep result that pins (a) the deployed plugin version, (b) the
    // tool surface count, and (c) the Node runtime version. The
    // WhatsApp keystones investigation cost two hours because we
    // couldn't confirm the customer was on v2.6.x without asking
    // them to cat package.json. This line makes that a one-grep
    // answer: ``grep "BOOT" /tmp/openclaw/openclaw-*.log``.
    try {
      const nodeVersion =
        typeof process !== "undefined" && process.versions
          ? process.versions.node
          : "unknown";
      console.log(
        `[memclaw] BOOT: plugin v${PLUGIN_VERSION}, ${MEMCLAW_TOOLS.length} tools registered, node ${nodeVersion}`,
      );
    } catch (e: unknown) {
      // Diagnostic line must never block registration — swallow any
      // exotic environment error (e.g. process is undefined in a
      // restricted worker).
      logError("boot diagnostic line failed", e);
    }

    // --- Educate gateway method ---
    api.registerGatewayMethod("memclaw.educate", ({ respond, params }: any) => {
      const { prompt, workspaces: filterWs } = params || {};
      if (!prompt || typeof prompt !== "string") {
        respond({ ok: false, error: "prompt is required" });
        return;
      }

      const openclawDir = getOpenClawBaseDir();
      if (!existsSync(openclawDir)) {
        respond({ ok: false, error: "~/.openclaw directory not found" });
        return;
      }

      const agentIds = Array.isArray(filterWs) ? filterWs : undefined;
      const educateResult = educateAgents(prompt, agentIds);

      if (educateResult.verified === 0) {
        respond({
          ok: false,
          error:
            educateResult.failed.length > 0
              ? `All writes failed: ${educateResult.failed.map((f) => `${f.workspace}: ${f.error}`).join("; ")}`
              : "No workspace directories found",
        });
        return;
      }

      respond({
        ok: true,
        message: `Education prompt written and verified in ${educateResult.verified} workspace(s)`,
        workspaces: educateResult.count,
        verified: educateResult.verified,
        educated: educateResult.educated,
        failed: educateResult.failed.length ? educateResult.failed : undefined,
      });
    });
  },
};

export default memclawPlugin;
