/**
 * OpenClaw configuration helpers and auto-fix allowlist.
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join } from "path";
import { MEMCLAW_TOOLS } from "./tools.js";
import { getPluginDir, getOpenClawConfigPath } from "./paths.js";
import { logError } from "./logger.js";

export { getPluginDir, getOpenClawConfigPath };

export function getPluginSrcPath(): string {
  return join(getPluginDir(), "src", "index.ts");
}

export function readOpenClawConfig(): Record<string, unknown> | null {
  const path = getOpenClawConfigPath();
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf-8"));
  } catch (e: unknown) {
    logError("Failed to parse openclaw.json", e);
    return null;
  }
}

// Using `any` for config parameter since openclaw.json has a dynamic schema
// that varies by version and cannot be statically typed here.
/* eslint-disable @typescript-eslint/no-explicit-any */

export function isMemclawAllowed(config: Record<string, any>): boolean {
  const allow = config?.plugins?.allow;
  return Array.isArray(allow) && allow.includes("memclaw");
}

export function isMemclawEnabled(config: Record<string, any>): boolean {
  return !!config?.plugins?.entries?.memclaw?.enabled;
}

export function isMemclawPathLoaded(config: Record<string, any>): boolean {
  const paths = config?.plugins?.load?.paths;
  const pluginDir = getPluginDir();
  return Array.isArray(paths) && paths.includes(pluginDir);
}

/**
 * True iff OpenClaw's exclusive memory slot is claimed by memclaw. The plugin
 * can be loaded and enabled but have another plugin hold the memory slot, in
 * which case `register()` runs but memory-runtime methods are never called.
 */
export function isMemorySlotClaimed(config: Record<string, any>): boolean {
  return config?.plugins?.slots?.memory === "memclaw";
}

/**
 * True iff OpenClaw's contextEngine slot is claimed by memclaw. The
 * contextEngine slot is what gates ``ContextEngine.assemble()`` — the
 * code path that injects ``<keystone_rules>`` into the system prompt
 * on every turn. Without this slot, OpenClaw falls back to the default
 * "legacy" engine and our ``assemble()`` never runs. The tool surface
 * (``memclaw_keystones``) still works because tool registration is
 * slot-independent — but the dynamic keystone injection silently dies.
 *
 * Confirmed against OpenClaw 2026.5.4
 * ``dist/registry-DFFgCbcm.js:241 resolveContextEngine``.
 */
export function isContextEngineSlotClaimed(config: Record<string, any>): boolean {
  return config?.plugins?.slots?.contextEngine === "memclaw";
}

export function isMemclawFullyConfigured(config: Record<string, any>): boolean {
  return (
    isMemclawAllowed(config) &&
    isMemclawEnabled(config) &&
    isMemclawPathLoaded(config) &&
    isMemorySlotClaimed(config) &&
    isContextEngineSlotClaimed(config)
  );
}

export function autoFixAllowlist(options?: {
  forceSlotOverride?: boolean;
}): {
  changed: boolean;
  changes: string[];
  error?: string;
} {
  const configPath = getOpenClawConfigPath();
  const config = readOpenClawConfig() as Record<string, any> | null;
  if (!config) {
    return {
      changed: false,
      changes: [],
      error: "openclaw.json not found at " + configPath,
    };
  }

  const changes: string[] = [];

  // 1. Ensure memclaw is in plugins.allow
  if (!isMemclawAllowed(config)) {
    if (!config.plugins) config.plugins = {};
    if (!Array.isArray(config.plugins.allow)) config.plugins.allow = [];
    config.plugins.allow.push("memclaw");
    changes.push("plugins.allow");
  }

  // 2. Ensure memclaw is enabled in plugins.entries
  if (!isMemclawEnabled(config)) {
    if (!config.plugins) config.plugins = {};
    if (!config.plugins.entries) config.plugins.entries = {};
    config.plugins.entries.memclaw = { enabled: true };
    changes.push("plugins.entries");
  }

  // 3. Ensure plugin path is in plugins.load.paths
  if (!isMemclawPathLoaded(config)) {
    if (!config.plugins) config.plugins = {};
    if (!config.plugins.load) config.plugins.load = {};
    if (!Array.isArray(config.plugins.load.paths))
      config.plugins.load.paths = [];
    config.plugins.load.paths.push(getPluginDir());
    changes.push("plugins.load.paths");
  }

  // 4. Claim the exclusive memory slot for memclaw
  if (!config.plugins) config.plugins = {};
  if (!config.plugins.slots) config.plugins.slots = {};
  if (config.plugins.slots.memory !== "memclaw") {
    const previousSlot = config.plugins.slots.memory;
    if (previousSlot && !options?.forceSlotOverride) {
      console.warn(
        `[memclaw] plugins.slots.memory already set to "${previousSlot}" — ` +
          `skipping auto-override. Run "openclaw gateway memclaw.allowlist.fix" ` +
          `or set MEMCLAW_AUTO_FIX_CONFIG=true to force.`,
      );
    } else {
      config.plugins.slots.memory = "memclaw";
      // Disable the previous memory plugin to avoid slot conflict
      if (previousSlot && config.plugins.entries?.[previousSlot]) {
        config.plugins.entries[previousSlot].enabled = false;
        changes.push(`disabled ${previousSlot}`);
      }
      changes.push(
        previousSlot
          ? `plugins.slots.memory (was: ${previousSlot})`
          : "plugins.slots.memory",
      );
    }
  }

  // 4b. Claim the contextEngine slot. Without this, OpenClaw falls back
  //     to the "legacy" default engine and our ContextEngine.assemble()
  //     never runs — so <keystone_rules> never appears in the system
  //     prompt. Mirrors step 4's forceSlotOverride handling so an
  //     operator who deliberately set a different engine doesn't get
  //     silently stomped.
  if (config.plugins.slots.contextEngine !== "memclaw") {
    const previousCe = config.plugins.slots.contextEngine;
    if (previousCe && !options?.forceSlotOverride) {
      console.warn(
        `[memclaw] plugins.slots.contextEngine already set to "${previousCe}" — ` +
          `skipping auto-override. Run "openclaw gateway memclaw.allowlist.fix" ` +
          `or set MEMCLAW_AUTO_FIX_CONFIG=true to force. ` +
          `Note: keystone rules will NOT inject without contextEngine="memclaw".`,
      );
    } else {
      config.plugins.slots.contextEngine = "memclaw";
      // Disable the previous contextEngine plugin to avoid slot conflict.
      // Without this, two plugins are both ``enabled`` and both
      // declared a contextEngine — OpenClaw's resolveContextEngine
      // reads the slot value and picks the matching engine, but the
      // OTHER plugin still registers its engine at load time, which is
      // either dead weight or a load-order race depending on the
      // runtime version. Mirrors step 4 (memory slot) exactly.
      if (previousCe && config.plugins.entries?.[previousCe]) {
        config.plugins.entries[previousCe].enabled = false;
        changes.push(`disabled ${previousCe}`);
      }
      changes.push(
        previousCe
          ? `plugins.slots.contextEngine (was: ${previousCe})`
          : "plugins.slots.contextEngine",
      );
    }
  }

  // 5. Ensure tools are in tools.alsoAllow
  if (!config.tools) config.tools = {};
  if (!Array.isArray(config.tools.alsoAllow)) config.tools.alsoAllow = [];
  for (const t of MEMCLAW_TOOLS) {
    if (!config.tools.alsoAllow.includes(t)) {
      config.tools.alsoAllow.push(t);
      changes.push(t);
    }
  }

  // 6. Remove stale pre-v1.0 tool names that no longer match any registered tool
  const staleRemoved: string[] = [];
  const currentToolSet = new Set<string>(MEMCLAW_TOOLS);
  config.tools.alsoAllow = config.tools.alsoAllow.filter((entry: string) => {
    if (entry.startsWith("memclaw_") && !currentToolSet.has(entry)) {
      staleRemoved.push(entry);
      return false;
    }
    return true;
  });
  if (staleRemoved.length > 0) {
    changes.push(`removed stale: ${staleRemoved.join(", ")}`);
  }

  if (changes.length === 0) return { changed: false, changes: [] };

  try {
    writeFileSync(
      configPath,
      JSON.stringify(config, null, 2) + "\n",
      "utf-8",
    );
    return { changed: true, changes };
  } catch (e: unknown) {
    const msg = logError("autoFixAllowlist write failed", e);
    return { changed: false, changes: [], error: msg };
  }
}

export function getMissingTools(config: Record<string, any>): string[] {
  const alsoAllow = config?.tools?.alsoAllow;
  if (!Array.isArray(alsoAllow)) return [...MEMCLAW_TOOLS];
  return MEMCLAW_TOOLS.filter((t) => !alsoAllow.includes(t));
}

/**
 * Decide whether ``autoFixAllowlist`` should run on this registration.
 *
 * Pure so it can be unit-tested. The original gate ran auto-fix exactly
 * once (guarded by the ``.allowlist-applied`` flag file), which meant a
 * plugin upgrade that ADDED a tool to ``MEMCLAW_TOOLS`` (e.g.
 * ``memclaw_keystones``) never got that tool into ``tools.alsoAllow`` on
 * an existing install — so a later OpenClaw ``tools.profile`` (which only
 * grants core tools + ``alsoAllow``) silently stripped it. We now also
 * re-run when there is drift: a missing tool or an unclaimed contextEngine
 * slot. ``autoFixAllowlist`` is idempotent (writes only on change) so a
 * clean install with the flag present still no-ops.
 *
 *   - ``MEMCLAW_AUTO_FIX_CONFIG=true``  → always run (explicit force).
 *   - ``MEMCLAW_AUTO_FIX_CONFIG=false`` → never run (explicit opt-out).
 *   - unset → run on first registration (no flag) OR when drift exists.
 */
export function shouldRunAutoFix(params: {
  autoFixEnv?: string;
  flagExists: boolean;
  missingToolCount: number;
  contextEngineSlotClaimed: boolean;
}): boolean {
  if (params.autoFixEnv === "true") return true;
  if (params.autoFixEnv === "false") return false;
  return (
    !params.flagExists ||
    params.missingToolCount > 0 ||
    !params.contextEngineSlotClaimed
  );
}
