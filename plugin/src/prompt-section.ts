/**
 * MemClaw Memory Prompt Section Builder
 *
 * Produces the system-prompt fragment that is injected on every turn
 * via the OpenClaw memory-prompt-section API. Two consumers:
 *
 *   1. registerMemoryPromptSection  — native path (requires kind:"memory")
 *   2. before_prompt_build hook     — fallback for older OpenClaw versions
 *
 * Per-turn token budget: this fragment is paid on every model call. The
 * deep reference (three rules, write triggers, capture cadence, quality,
 * prohibitions, per-tool signatures) lives in `skills/memclaw/SKILL.md`
 * and is loaded on demand via the skill `read` path. The per-workspace
 * `TOOLS.md` and `AGENTS.md` carry the slim every-turn cues. This file
 * is intentionally minimal: a header, the available tool list, the
 * non-negotiable identity reminder, and a pointer to SKILL.md.
 *
 * On hosts that do NOT bootstrap-inject TOOLS.md / AGENTS.md, this
 * fragment is the agent's only direct prompt-time signal that MemClaw
 * exists; the agent is still expected to read SKILL.md before its
 * first MemClaw call to pick up the operating contract.
 */

import { MEMCLAW_TOOLS } from "./tools.js";

function buildRecallLines(availableTools: Set<string>): string[] {
  const present = MEMCLAW_TOOLS.filter((t) => availableTools.has(t));
  if (present.length === 0) return [];

  const lines: string[] = [];
  lines.push("## MemClaw Memory");
  lines.push("");
  lines.push(
    "Persistent cross-session memory. Available tools: " +
      present.join(", ") +
      ". Every call MUST carry your `agent_id` (and `fleet_id` for " +
      "team / org / cross-fleet operations) — never fabricate. " +
      "Operating rules (recall before, write after, supersede don't " +
      "delete), quality rules, and per-tool reference live in " +
      "`skills/memclaw/SKILL.md` — read it before your first MemClaw " +
      "call this session.",
  );
  lines.push("");

  return lines;
}

/**
 * MemoryPromptSectionBuilder-compatible function.
 *
 * Signature matches OpenClaw's expected:
 *   ({ availableTools: Set<string>, citationsMode?: string }) => string[]
 */
export function memclawPromptSectionBuilder(params: {
  availableTools: Set<string>;
  citationsMode?: string;
}): string[] {
  return buildRecallLines(params.availableTools);
}

/**
 * Flatten the prompt section into a single string for use as
 * `prependSystemContext` in the before_prompt_build fallback path.
 */
export function memclawPromptSectionText(
  availableTools: Set<string>,
): string {
  return buildRecallLines(availableTools).join("\n");
}
