/**
 * Plugin deployment logic.
 *
 * Security controls:
 * - Size validation on source code (MAX_SOURCE_SIZE)
 * - Only MEMCLAW_* env vars accepted (key filtering)
 * - Backup and restore on build failure
 *
 * Note: caller authentication (HMAC or token) is handled upstream —
 * in heartbeat.ts processCommand() and index.ts gateway method.
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join } from "path";
import { execSync } from "child_process";
import { getPluginDir, getPluginSrcPath } from "./config.js";
import { BUILD_TIMEOUT_MS, MAX_SOURCE_SIZE } from "./env.js";
import { logError } from "./logger.js";

export async function deployPlugin(
  source: string,
  envVars?: Record<string, string>,
): Promise<{
  ok: boolean;
  error?: string;
  buildOutput?: string;
  envUpdated?: string[];
}> {
  if (source.length > MAX_SOURCE_SIZE) {
    return { ok: false, error: `source too large (${source.length} bytes, max ${MAX_SOURCE_SIZE})` };
  }

  const pluginDir = getPluginDir();
  const srcPath = getPluginSrcPath();

  try {
    // 1. Backup current source
    if (existsSync(srcPath)) {
      writeFileSync(srcPath + ".bak", readFileSync(srcPath, "utf-8"), "utf-8");
    }

    // 2. Write new source
    writeFileSync(srcPath, source, "utf-8");

    // 3. Merge env vars into existing .env file if provided
    const envChanges: string[] = [];
    if (envVars && typeof envVars === "object") {
      const envPath = join(pluginDir, ".env");

      // Read existing .env into a Map to preserve keys not being updated
      const existing = new Map<string, string>();
      if (existsSync(envPath)) {
        for (const line of readFileSync(envPath, "utf-8").split("\n")) {
          const trimmed = line.trim();
          if (!trimmed || trimmed.startsWith("#")) continue;
          const eq = trimmed.indexOf("=");
          if (eq < 1) continue;
          const k = trimmed.slice(0, eq).trim();
          const v = trimmed.slice(eq + 1).trim();
          if (/^MEMCLAW_/.test(k)) existing.set(k, v);
        }
      }

      // Merge provided keys over existing
      for (const [key, val] of Object.entries(envVars)) {
        if (/^MEMCLAW_/.test(key) && typeof val === "string") {
          existing.set(key, val.replace(/[\r\n]/g, ""));
          envChanges.push(key);
        }
      }

      // Write merged result only if there were actual changes
      if (envChanges.length > 0) {
        const lines = Array.from(existing, ([k, v]) => `${k}=${v}`);
        writeFileSync(envPath, lines.join("\n") + "\n", "utf-8");
      }
    }

    // 4. Run build
    const buildOutput = execSync("npm run build 2>&1", {
      cwd: pluginDir,
      encoding: "utf-8",
      timeout: BUILD_TIMEOUT_MS,
    });

    return {
      ok: true,
      buildOutput: buildOutput.slice(-5000),
      envUpdated: envChanges,
    };
  } catch (e: unknown) {
    // Build failed — restore backup
    const bakPath = srcPath + ".bak";
    if (existsSync(bakPath)) {
      try {
        writeFileSync(srcPath, readFileSync(bakPath, "utf-8"), "utf-8");
      } catch (restoreErr: unknown) {
        logError("Failed to restore backup", restoreErr);
      }
    }

    const err = e as Error & { stdout?: string; stderr?: string };
    return {
      ok: false,
      error: "Deploy failed: " + (err.message || "unknown error"),
      buildOutput: (err.stdout || err.stderr || "").slice(-5000),
    };
  }
}
