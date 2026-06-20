/**
 * OpenClaw configuration helpers and auto-fix allowlist.
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join, resolve } from "path";
import { homedir } from "os";
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
  // CAURA-000: `plugins.allow` in OpenClaw 2026.6.x is a STRICT
  // allowlist only when it is BOTH present AND non-empty. The runtime
  // gate is:
  //
  //     entry?.enabled === true
  //       && (plugins.allow.length === 0 || plugins.allow.includes(pluginId))
  //
  // (from OpenClaw's selection layer — confirmed against
  // `/usr/lib/node_modules/openclaw/dist/*.js` on the wet-test VM).
  //
  // So a missing OR empty `plugins.allow` means "no restriction —
  // every enabled plugin can load". Memclaw doesn't need to appear
  // in the array in those cases; it's allowed by default. Pre-fix this
  // function returned `false` for missing/empty allow, which drove
  // `autoFixAllowlist` step 1 to *create* an array containing only
  // `"memclaw"` — flipping the user's permissive config into a
  // restrictive one and silently locking out every other built-in
  // plugin including `openai`. Customer-reported on a fresh 2.8.1
  // install: "Unknown model: openai/gpt-5.5" 2 minutes after install.
  if (!Array.isArray(allow) || allow.length === 0) return true;
  return allow.includes("memclaw");
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

  // 1. Ensure memclaw is in `plugins.allow` IF — and only if — the
  //    user has an explicit, non-empty allowlist. CAURA-000: pre-fix
  //    this branch unconditionally created `["memclaw"]` from a
  //    missing/empty array, converting a permissive OpenClaw config
  //    into a restrictive one that locked out every other plugin
  //    (the customer's "openai disabled after 2.8.1 install"
  //    symptom). The runtime gate treats missing/empty allow as
  //    "no restriction" — see `isMemclawAllowed` docstring for the
  //    OpenClaw-side gate evidence.
  //
  //    So the fix is: leave a missing/empty allowlist alone; only
  //    append to one that's already non-empty (i.e. the user has
  //    explicitly opted into the allowlist mechanism and we need to
  //    make sure memclaw is in their list).
  if (
    Array.isArray(config?.plugins?.allow) &&
    config.plugins.allow.length > 0 &&
    !config.plugins.allow.includes("memclaw")
  ) {
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

/** Canonicalize a config entry for dedup comparison against our
 * already-absolute target dirs. ``~``/``~/`` expand to the home dir;
 * absolute paths are resolved; a RELATIVE entry is compared literally —
 * resolving it against ``process.cwd()`` would guess a base that likely
 * differs from how OpenClaw interprets relative ``extraDirs`` entries,
 * causing false dedup misses. */
function canonicalDir(p: string): string {
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return resolve(join(homedir(), p.slice(2)));
  if (p.startsWith("/")) return resolve(p);
  return p; // relative path: compare literally, don't guess a base
}

/**
 * Ensure each dir in ``dirs`` is present in ``skills.load.extraDirs`` in
 * ``openclaw.json`` — OpenClaw's documented, watched load path for extra
 * skill directories (``docs/tools/skills-config.md``; consumed by
 * ``src/skills/runtime/refresh.ts``). This is how a reconciled *additive*
 * target dir (one MemClaw doesn't own and can't publish as a plugin skill)
 * actually reaches agents.
 *
 * Append-only and idempotent: existing entries are preserved, a dir already
 * present (compared by canonical path, so ``~`` entries match) is left
 * alone, and the file is written ONLY when something was added. Mirrors the
 * ``autoFixAllowlist`` write idiom and OpenClaw's own
 * ``plugins-install-command`` extraDirs-merge pattern. Fails safe: a missing
 * or unreadable config, or a write error, returns an ``error`` and never
 * throws — the heartbeat must not crash on a registration failure.
 *
 * NOTE: adding a dir here makes its skills discoverable on the node, but an
 * already-running agent session keeps its cached ``<available_skills>``
 * snapshot until a fresh session starts.
 */
export function ensureExtraSkillDirs(dirs: string[]): {
  changed: boolean;
  added: string[];
  /** Wanted dirs already on the load path before this call — genuinely
   * registered regardless of whether a write for new additions failed. */
  alreadyPresent: string[];
  error?: string;
} {
  const wanted = [...new Set(dirs.filter((d) => typeof d === "string" && d.trim()))];
  if (wanted.length === 0) return { changed: false, added: [], alreadyPresent: [] };

  const config = readOpenClawConfig() as Record<string, any> | null;
  if (!config) {
    return {
      changed: false,
      added: [],
      alreadyPresent: [],
      error: `openclaw.json not found or unreadable at ${getOpenClawConfigPath()}`,
    };
  }
  // Must be a JSON object. A top-level array (or other non-object) is
  // truthy and would slip past a bare ``!config`` check — then setting
  // ``.skills`` on it is silently dropped by JSON.stringify and the file
  // gets rewritten as ``[]``. Reject it instead of clobbering the config.
  if (typeof config !== "object" || Array.isArray(config)) {
    return {
      changed: false,
      added: [],
      alreadyPresent: [],
      error: `openclaw.json is not a JSON object at ${getOpenClawConfigPath()}`,
    };
  }

  // Normalize the nested containers, but only when ABSENT (null/undefined).
  // A truthy non-object (e.g. ``"skills": "foo"`` or ``42``) is a malformed
  // config we must not silently overwrite — detect-and-error, consistent
  // with the top-level guard above.
  if (config.skills == null) {
    config.skills = {};
  } else if (typeof config.skills !== "object" || Array.isArray(config.skills)) {
    return {
      changed: false,
      added: [],
      alreadyPresent: [],
      error: `openclaw.json 'skills' is not an object at ${getOpenClawConfigPath()}`,
    };
  }
  if (config.skills.load == null) {
    config.skills.load = {};
  } else if (typeof config.skills.load !== "object" || Array.isArray(config.skills.load)) {
    return {
      changed: false,
      added: [],
      alreadyPresent: [],
      error: `openclaw.json 'skills.load' is not an object at ${getOpenClawConfigPath()}`,
    };
  }
  // Preserve the original array verbatim on write (including any non-string
  // entries — future format extensions or user mistakes); use the
  // string-only view solely for canonical-path dedup.
  const originalExtraDirs: unknown[] = Array.isArray(config.skills.load.extraDirs)
    ? config.skills.load.extraDirs
    : [];
  const existing: string[] = originalExtraDirs.filter(
    (x): x is string => typeof x === "string",
  );
  const present = new Set(existing.map(canonicalDir));
  const alreadyPresent: string[] = wanted.filter((d) => present.has(canonicalDir(d)));

  const added: string[] = [];
  const next: unknown[] = [...originalExtraDirs];
  for (const dir of wanted) {
    if (present.has(canonicalDir(dir))) continue;
    next.push(dir);
    present.add(canonicalDir(dir));
    added.push(dir);
  }
  if (added.length === 0) return { changed: false, added: [], alreadyPresent };

  config.skills.load.extraDirs = next;
  try {
    writeFileSync(getOpenClawConfigPath(), JSON.stringify(config, null, 2) + "\n", "utf-8");
    return { changed: true, added, alreadyPresent };
  } catch (e: unknown) {
    const msg = logError("ensureExtraSkillDirs write failed", e);
    return { changed: false, added: [], alreadyPresent, error: msg };
  }
}
