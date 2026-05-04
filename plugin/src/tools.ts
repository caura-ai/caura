/**
 * Ordered list of MemClaw tool names exposed via the OpenClaw plugin.
 *
 * The *set* is derived from `plugin/tools.json` (SoT): every entry with
 * `plugin_exposed: true`. The *order* is hand-maintained here because
 * it is observable — it drives registration order, the "Available
 * MemClaw tools: …" line in the prompt section, and any downstream UI
 * that renders tools in discovery order.
 *
 * Current surface: 12 tools — LTM (write/recall/list/manage), doc,
 * entity_get, tune, insights, evolve, stats, share_skill, unshare_skill.
 * STM tools are not exposed via the plugin.
 *
 * A boot-time drift check throws if this list and tools.json disagree.
 */
import { TOOL_SPECS } from "./tool-specs.js";

export const MEMCLAW_TOOLS = [
  "memclaw_recall",
  "memclaw_write",
  "memclaw_manage",
  "memclaw_doc",
  "memclaw_list",
  "memclaw_entity_get",
  "memclaw_tune",
  "memclaw_insights",
  "memclaw_evolve",
  "memclaw_stats",
  "memclaw_share_skill",
  "memclaw_unshare_skill",
] as const;

// --- Boot-time drift check ---

const exposedInSpec = new Set(
  TOOL_SPECS.filter((t) => t.plugin_exposed).map((t) => t.name),
);
const listed = new Set<string>(MEMCLAW_TOOLS);

const missingFromList = [...exposedInSpec].filter((t) => !listed.has(t));
const extraInList = [...listed].filter((t) => !exposedInSpec.has(t));

if (missingFromList.length || extraInList.length) {
  const parts: string[] = [];
  if (missingFromList.length) {
    parts.push(
      `exposed in tools.json but missing from MEMCLAW_TOOLS: ${missingFromList.join(", ")}`,
    );
  }
  if (extraInList.length) {
    parts.push(
      `listed in MEMCLAW_TOOLS but not plugin_exposed in tools.json: ${extraInList.join(", ")}`,
    );
  }
  throw new Error(`[memclaw] Tool-surface drift — ${parts.join("; ")}`);
}
