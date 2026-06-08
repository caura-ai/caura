/**
 * Typed loader for `plugin/tools.json` — the single source of truth for
 * MemClaw MCP tool metadata, exported from core-api by
 * `scripts/export_tool_specs.py` and kept in sync via CI.
 *
 * This module reads the file at runtime (not at compile time) because
 * `tools.json` lives outside `tsconfig`'s `rootDir`. It resolves relative
 * to this module's own URL so it works in dev (`src/`) and in the built
 * artifact (`dist/`).
 */

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

export interface ToolParamSpec {
  name: string;
  type: string;
  required: boolean;
  default?: unknown;
  description?: string;
}

export interface ToolSpecEntry {
  name: string;
  description: string;
  error_codes: string[];
  impl_status: "live" | "reserved" | "deprecated";
  ops: Array<Record<string, unknown>>;
  params: ToolParamSpec[];
  plugin_exposed: boolean;
  trust_required: number;
}

// Dev:  __dirname = plugin/src  → ../tools.json = plugin/tools.json
// Prod: __dirname = plugin/dist → ../tools.json = plugin/tools.json
const here = dirname(fileURLToPath(import.meta.url));
const toolsJsonPath = join(here, "..", "tools.json");

export const TOOL_SPECS: readonly ToolSpecEntry[] = Object.freeze(
  JSON.parse(readFileSync(toolsJsonPath, "utf-8")) as ToolSpecEntry[],
);

const byName: Record<string, ToolSpecEntry> = Object.fromEntries(
  TOOL_SPECS.map((t) => [t.name, t]),
);

export const TOOL_SPECS_BY_NAME: Readonly<Record<string, ToolSpecEntry>> =
  Object.freeze(byName);

export function getSpec(name: string): ToolSpecEntry {
  const spec = TOOL_SPECS_BY_NAME[name];
  if (!spec) {
    throw new Error(
      `[memclaw] Unknown tool '${name}' — not present in tools.json`,
    );
  }
  return spec;
}
