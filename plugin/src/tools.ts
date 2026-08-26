/**
 * Ordered list of Caura tool names exposed via the OpenClaw plugin.
 *
 * The *set* is derived from `plugin/tools.json` (SoT): every entry with
 * `plugin_exposed: true`. The *order* is hand-maintained here because
 * it is observable — it drives registration order, the "Available
 * tools: …" line in the prompt section, and any downstream UI
 * that renders tools in discovery order.
 *
 * Current surface: 11 tools — LTM (write/recall/list/manage), doc,
 * entity_get, tune, insights, evolve, stats, plus keystones (read-only
 * governance rules surfaced to agents at session start). Skill sharing
 * is done via caura_doc with collection="skills". STM tools and
 * caura_keystones_set (admin authoring path, ``plugin_exposed=false``)
 * are not surfaced via the plugin.
 *
 * A boot-time drift check throws if this list and tools.json disagree.
 */
import { TOOL_SPECS } from "./tool-specs.js";

export const CAURA_TOOLS = [
  "caura_recall",
  "caura_write",
  "caura_manage",
  "caura_doc",
  "caura_list",
  "caura_entity_get",
  "caura_tune",
  "caura_insights",
  "caura_evolve",
  "caura_stats",
  "caura_keystones",
] as const;

// --- Boot-time drift check ---

const exposedInSpec = new Set(
  TOOL_SPECS.filter((t) => t.plugin_exposed).map((t) => t.name),
);
const listed = new Set<string>(CAURA_TOOLS);

const missingFromList = [...exposedInSpec].filter((t) => !listed.has(t));
const extraInList = [...listed].filter((t) => !exposedInSpec.has(t));

if (missingFromList.length || extraInList.length) {
  const parts: string[] = [];
  if (missingFromList.length) {
    parts.push(
      `exposed in tools.json but missing from CAURA_TOOLS: ${missingFromList.join(", ")}`,
    );
  }
  if (extraInList.length) {
    parts.push(
      `listed in CAURA_TOOLS but not plugin_exposed in tools.json: ${extraInList.join(", ")}`,
    );
  }
  throw new Error(`[caura] Tool-surface drift — ${parts.join("; ")}`);
}
