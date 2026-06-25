/**
 * Agent education — write education prompts to HEARTBEAT.md in agent
 * workspaces, and append MemClaw sections to TOOLS.md and AGENTS.md.
 *
 * SKILL.md is no longer written per-workspace: it ships as a static file at
 * `<plugin-root>/skills/memclaw/SKILL.md` and is discovered by OpenClaw via
 * the `skills` field in `openclaw.plugin.json`.
 *
 * Security fixes:
 * - Path containment check prevents traversal attacks
 * - Prompt length cap prevents disk exhaustion
 */

import {
  readFileSync,
  writeFileSync,
  existsSync,
  readdirSync,
  statSync,
  unlinkSync,
} from "fs";
import { join, basename, resolve } from "path";
import { createHash } from "crypto";
import { isContainedPath, assertPromptLength } from "./validation.js";
import { getOpenClawBaseDir } from "./paths.js";
import { MEMCLAW_TOOLS } from "./tools.js";
import { logError } from "./logger.js";

/**
 * Resolve all agent workspace directories using the canonical 4-source
 * discovery, deduplicated and existence-filtered:
 *
 *  1. <baseDir>/workspace                — default workspace, id="main"
 *  2. <baseDir>/workspace-<name>         — hyphen-prefix at baseDir, id=<name>
 *  3. agents.list[].workspace            — from openclaw.json (canonical),
 *                                          id from agent.id || agent.name
 *  4. <baseDir>/workspaces/<name>/       — subdir of plural parent,
 *                                          id=<name>; this is the path
 *                                          `openclaw agents add --workspace`
 *                                          places workspaces under by default.
 *
 * Path containment: every resolved path must live inside baseDir.
 * Dedup: identical resolved paths are returned once (first source wins).
 * Filter: when `agentIds` is non-empty, only entries whose id is in the
 *         set are returned.
 *
 * Replaces the previous `readdirSync(baseDir).filter(startsWith("workspace"))`
 * walk used by writeEducationFiles, which (a) missed agents under
 * `<baseDir>/workspaces/<name>/`, and (b) treated the literal `workspaces`
 * plural dir as a single workspace, producing phantom education files.
 */
export function discoverAgentWorkspaces(
  baseDir: string,
  agentIds?: string[],
): Array<{ path: string; id: string }> {
  const filterSet = agentIds?.length ? new Set(agentIds) : null;
  const seen = new Set<string>();
  const results: Array<{ path: string; id: string }> = [];

  function addWs(dir: string, id: string): void {
    if (!id) return;
    const resolved = dir.startsWith("/") ? resolve(dir) : resolve(join(baseDir, dir));
    if (!isContainedPath(resolved, baseDir)) {
      console.warn(
        `[memclaw] Rejected workspace path outside openclawDir: ${resolved}`,
      );
      return;
    }
    if (seen.has(resolved) || !existsSync(resolved)) return;
    try {
      if (!statSync(resolved).isDirectory()) return;
    } catch {
      return;
    }
    if (filterSet && !filterSet.has(id)) return;
    seen.add(resolved);
    results.push({ path: resolved, id });
  }

  // 1. Default workspace
  addWs(join(baseDir, "workspace"), "main");

  // 2. Hyphen-prefix at baseDir: <baseDir>/workspace-<name>
  try {
    for (const entry of readdirSync(baseDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (!entry.name.startsWith("workspace-")) continue;
      const id = entry.name.replace(/^workspace-/, "");
      addWs(join(baseDir, entry.name), id);
    }
  } catch (e: unknown) {
    logError("Failed to scan baseDir for hyphen-prefix workspaces", e);
  }

  // 3. agents.list[].workspace from openclaw.json
  try {
    const configPath = join(baseDir, "openclaw.json");
    if (existsSync(configPath)) {
      const config = JSON.parse(readFileSync(configPath, "utf-8"));
      const agentList = config?.agents?.list;
      if (Array.isArray(agentList)) {
        for (const agent of agentList) {
          if (agent.workspace) {
            addWs(agent.workspace, agent.id || agent.name);
          }
        }
      }
    }
  } catch (e: unknown) {
    logError("Failed to read agent config in discoverAgentWorkspaces", e);
  }

  // 4. <baseDir>/workspaces/<name>/ — used by `openclaw agents add`
  const wsParent = join(baseDir, "workspaces");
  if (existsSync(wsParent)) {
    try {
      if (statSync(wsParent).isDirectory()) {
        for (const entry of readdirSync(wsParent, { withFileTypes: true })) {
          if (!entry.isDirectory()) continue;
          addWs(join(wsParent, entry.name), entry.name);
        }
      }
    } catch (e: unknown) {
      logError("Failed to scan workspaces/ subdirectories", e);
    }
  }

  return results;
}

/**
 * Strip the legacy DEFAULT_EDUCATION paragraph from a HEARTBEAT.md
 * string, if present. Pre-C1 the plugin wrote a 4-sentence MemClaw
 * intro into each workspace's HEARTBEAT.md on first install:
 *
 *   You have been connected to MemClaw — a shared persistent memory
 *   system. You now have N tools for writing, searching, and managing
 *   memories. Check skills/memclaw/SKILL.md for full instructions.
 *   Key rules: always search before starting work, always write
 *   findings after completing work, always include your agent_id.
 *
 * That paragraph is fully redundant with TOOLS.md / AGENTS.md /
 * SKILL.md and the system-prompt section, has no fence (so plugin
 * upgrades never refreshed it), and frozen the tool count at write
 * time. We no longer write it (auto-education stops at TOOLS.md /
 * AGENTS.md), and this helper one-shot cleans it from any
 * HEARTBEAT.md left over from a prior install.
 *
 * Match strategy: anchor on the unique opening clause ("You have been
 * connected to MemClaw") and the unique closing clause ("always
 * include your agent_id."), single-line `[^\n]*?` between. Optional
 * leading separator pattern `\n+---\n+` — flexible enough to match
 * both the canonical `\n\n---\n\n` (which is what `educateAgents`
 * writes when prepending to non-empty content) AND the reduced
 * `\n---\n\n` form that's left behind when an *earlier* paragraph
 * already consumed one of the two leading newlines on its own
 * trailing-`\n?`.
 *
 * Loops until no more matches are found. A single call therefore
 * cleans **all** stale paragraphs in one pass, including the
 * pathological case (seen in the wild on 0.98.5 → 1.0 → 2.0
 * upgrade-chain installs) where the file accumulated multiple
 * paragraphs across reinstalls because `.educated` was deleted
 * between them. Without the loop, two-paragraph files would leave a
 * stray `---` rule between strips.
 *
 * Returns `{ content, cleaned }`; `cleaned` is true if any paragraph
 * was removed.
 *
 * The paragraph was always written as a single line by `educateAgents`
 * (the `prompt.trim()` it stores is the plain `DEFAULT_EDUCATION`
 * string with no internal newlines). The `[^\n]*?` middle is
 * intentionally single-line and will silently no-op if a future
 * version of the content was reflowed across multiple lines — do NOT
 * "fix" the regex to be multi-line (`[\s\S]*?`) without re-thinking
 * anchors, because greedy multi-line matching could span unrelated
 * user prose between two coincidental anchor occurrences and delete
 * it.
 */
export function cleanupStaleHeartbeatEducation(
  content: string,
): { content: string; cleaned: boolean } {
  // `\n{1,2}` (not `\n+`) on each side of the rule: bounded to one or
  // two newlines so we never eat a trailing newline from preceding
  // user content. The canonical separator written by `educateAgents`
  // is `\n\n---\n\n` (2+2); after a previous strip leaves a reduced
  // form `\n---\n\n` (1+2), this still matches.
  const re =
    /(?:\n{1,2}---\n{1,2})?You have been connected to MemClaw[^\n]*?always include your agent_id\.\n?/;
  let cur = content;
  let cleaned = false;
  // Bounded loop: stale paragraphs accumulate one per upgrade. A few
  // iterations is normal; an upper bound prevents infinite looping if
  // the regex were ever to match the empty string. Each iteration
  // makes monotonic progress (slice removes at least one char).
  for (let i = 0; i < 32; i++) {
    const m = cur.match(re);
    if (!m || m[0].length === 0) break;
    cur = cur.slice(0, m.index!) + cur.slice(m.index! + m[0].length);
    cleaned = true;
  }
  return { content: cur, cleaned };
}

/**
 * Delete orphan TOOLS.md / AGENTS.md at <baseDir>/workspaces/ that the
 * pre-fix discovery wrote when it mistakenly treated the plural parent
 * directory as a single workspace. Idempotent: missing files are no-op,
 * non-MemClaw content at that path is left alone.
 */
function cleanupPhantomEducationFiles(baseDir: string): void {
  const phantomDir = join(baseDir, "workspaces");
  for (const fname of ["TOOLS.md", "AGENTS.md"]) {
    const fpath = join(phantomDir, fname);
    if (!existsSync(fpath)) continue;
    try {
      const content = readFileSync(fpath, "utf-8");
      const isMemClawOrphan =
        (fname === "TOOLS.md" && content.includes("MemClaw — Tools Available")) ||
        (fname === "AGENTS.md" && content.includes("## Memory V2"));
      if (isMemClawOrphan) {
        unlinkSync(fpath);
      }
    } catch (e: unknown) {
      logError(`Failed to clean phantom education file ${fpath}`, e);
    }
  }
}

export function educateAgents(
  prompt: string,
  agentIds?: string[],
  baseDir?: string,
): {
  count: number;
  educated: string[];
  failed: Array<{ workspace: string; error: string }>;
  verified: number;
} {
  assertPromptLength(prompt);

  const openclawDir = baseDir || getOpenClawBaseDir();
  const wsList = discoverAgentWorkspaces(openclawDir, agentIds);

  let count = 0;
  let verified = 0;
  const educated: string[] = [];
  const failed: Array<{ workspace: string; error: string }> = [];

  for (const { path: wsDir } of wsList) {
    const wsName = basename(wsDir);
    const hbPath = join(wsDir, "HEARTBEAT.md");
    try {
      const existing = existsSync(hbPath)
        ? readFileSync(hbPath, "utf-8").trim()
        : "";

      // Idempotency: skip if prompt is already present
      if (existing && existing.includes(prompt.trim())) {
        verified++;
        count++;
        educated.push(wsName);
        continue;
      }

      const newContent = existing
        ? existing + "\n\n---\n\n" + prompt.trim() + "\n"
        : prompt.trim() + "\n";

      // Size cap: prevent unbounded growth from repeated educate calls
      const MAX_HEARTBEAT_SIZE = 256 * 1024;
      if (newContent.length > MAX_HEARTBEAT_SIZE) {
        failed.push({ workspace: wsName, error: "HEARTBEAT.md would exceed 256KB limit" });
        continue;
      }

      writeFileSync(hbPath, newContent, "utf-8");

      const readBack = readFileSync(hbPath, "utf-8");
      if (readBack.includes(prompt.trim())) {
        verified++;
        count++;
        educated.push(wsName);
      } else {
        failed.push({
          workspace: wsName,
          error: "Write succeeded but verification failed",
        });
      }
    } catch (e: unknown) {
      const msg = logError(`educate write failed for ${wsName}`, e);
      failed.push({ workspace: wsName, error: msg });
    }
  }

  return { count, educated, failed, verified };
}

// --- Education file builders ---
//
// Content is intentionally transport-neutral: no host ("plugin" / "gateway" /
// host-product) references, no env-var interpolation, no version tag.
// These files are the canonical MemClaw agent education payload and may be
// loaded by host-managed workspaces or served to MCP/REST callers through
// other transports.
//
// Role separation — cost-aware split between per-turn and on-demand files.
//
//   SKILL.md   — static file, shipped at `<plugin-root>/skills/memclaw/`
//                via `openclaw.plugin.json:skills`. Loaded by the model
//                via the `read` tool ON DEMAND; only the skill-list entry
//                (name + description + path) appears in every turn. Owns
//                the deep reference: mental model, identity, three rules,
//                trust levels, sharing semantics, container choice,
//                quality, session loop, per-tool signatures, decision
//                guidance, constraints, and error codes.
//
//   TOOLS.md   — per-workspace append, INJECTED EVERY TURN as bootstrap
//                (subject to 12 K per-file / 60 K total char caps). Kept
//                lean on purpose: quick matrix (per-tool purpose ×
//                returns) + enum vocabulary table + pointer to SKILL.md.
//                Retained because sub-agent sessions get only AGENTS.md
//                and TOOLS.md (other bootstrap files are filtered out),
//                so the enum vocab must be reachable without requiring
//                a SKILL.md read.
//
//   AGENTS.md  — per-workspace append, INJECTED EVERY TURN as bootstrap.
//                Behavioral enforcement: identity mandate, completion
//                contract, write triggers, capture cadences, quality
//                enforcement, prohibited behaviors. Short supersession
//                paragraph instructs the model to read SKILL.md before
//                its first MemClaw call.
//
// AGENTS.md idempotency is keyed off the substring "## Memory V2" in
// writeEducationFiles() below; buildAgentsMd() must continue to emit that
// exact substring.

export function buildToolsMd(): string {
  return `
---

## MemClaw — Tools Available

Persistent, cross-session, multi-agent memory. For per-tool signatures,
decision guidance, constraints, and error codes, open the **memclaw**
skill (your runtime loads it automatically — do NOT search the
filesystem for it) before your first call in a session.

\`agent_id\` is resolved by your runtime — never fabricate.

### Quick matrix · ${MEMCLAW_TOOLS.length} tools

| Tool | Purpose | Returns |
|------|---------|---------|
| \`memclaw_recall\`     | Semantic + keyword search                           | \`[{id, content, score, memory_type, …}]\` |
| \`memclaw_write\`      | Store one (\`content\`) or batch ≤100 (\`items\`)       | \`{id}\` or \`{ids[]}\` |
| \`memclaw_manage\`     | Per-memory: read / update / transition / delete     | op-dispatched |
| \`memclaw_list\`       | Non-semantic browse (filter, sort, paginate)        | \`{results[], cursor}\` |
| \`memclaw_doc\`        | Structured-doc CRUD in named collections            | op-dispatched |
| \`memclaw_entity_get\` | Entity by UUID                                      | \`{entity}\` |
| \`memclaw_tune\`       | Update retrieval profile (sticky, not per-call)     | current profile |
| \`memclaw_insights\`   | Reflect: contradictions / failures / patterns / …   | stored as \`insight\` memories |
| \`memclaw_evolve\`     | Report outcome after acting on recalled memories    | weight updates; may create rules |
| \`memclaw_stats\`      | Aggregate counts: total + by type/agent/status      | \`{total, by_type, by_agent, by_status, scope}\` |
| \`memclaw_keystones\`  | Read mandatory governance rules (auto-injected at session start) | \`{count, truncated, rules[]}\` |

### Vocabulary

Enum values here mirror the JSON Schema in the registered tools; keep
in sync.

| Field | Valid values |
|-------|--------------|
| \`memory_type\` (auto on write; filter on read) | \`fact\`, \`episode\`, \`decision\`, \`preference\`, \`task\`, \`semantic\`, \`intention\`, \`plan\`, \`commitment\`, \`action\`, \`outcome\`, \`cancellation\`, \`rule\`, \`insight\` |
| \`status\` (via \`memclaw_manage op=transition\`) | \`active\`, \`pending\`, \`confirmed\`, \`cancelled\`, \`outdated\`, \`conflicted\`, \`archived\`, \`deleted\` |
| \`visibility\` (write-time) | \`scope_agent\` · \`scope_team\` *(default)* · \`scope_org\` |
| \`scope\` (read-time on \`_list\`, \`_insights\`) | \`agent\` *(default)* · \`fleet\` · \`all\` |
| \`fleet_ids\` (optional recall filter) | array of fleet ID strings; narrows recall to those fleets (trust 2 for cross-fleet) |
| \`write_mode\` | \`fast\` · \`auto\` *(default)* · \`strong\` |
| \`focus\` (\`_insights\`) | \`contradictions\` · \`failures\` · \`stale\` · \`divergence\` · \`patterns\` · \`discover\` |
| \`outcome_type\` (\`_evolve\`) | \`success\` · \`failure\` · \`partial\` |
`;
}

export function buildAgentsMd(): string {
  return `

---

## Memory V2 — MemClaw Protocol (mandatory)

Supersedes any earlier memory instructions. MemClaw is the primary
persistent, cross-session, multi-agent memory. Any workspace file
(\`MEMORY.md\`, \`memory.md\`, etc.) is a session-local scratchpad —
keep it lean (active projects + current routing + recent decisions
≤ 7 days, target a few KB). Anything historical, factual, or useful
to other agents → write it to MemClaw.

**Identity.** Every call MUST carry your correct \`agent_id\` (and
\`fleet_id\` for team/org visibility, fleet-scoped reads, and cross-fleet
operations). Never fabricate. If uncertain, write privately
(\`visibility=scope_agent\`) until resolved.

**What to write.** Durable knowledge, not activity. Tool calls, command
output, and intermediate steps are scratchpad, not memories — they
pollute recall. Write only what helps another agent in a *later
session*; if it dies with this task, don't.

**Write triggers.** Task done · bug · deploy · decision · API change ·
blocker · commitment · config change · error pattern · skill created
or updated. If in doubt, don't.

**Skills.** Team-knowledge catalog at \`collection=skills\`. Search via
\`memclaw_doc op=search collection=skills\` (\`memclaw_recall\` is YOUR
memories, not shared); share via \`op=write doc_id=<slug>\`.

**Recall auto-gated** on trivial turns; call \`memclaw_recall\`
directly when a short message needs LTM.

Before your first MemClaw call this session, open the **memclaw**
skill — your runtime loads it automatically, so do NOT search the
filesystem for it — for signatures, cadences, quality, prohibitions,
recall policy, and sharing. \`TOOLS.md\` carries the at-a-glance tool
list and enum vocabulary every turn.
`;
}

// --- Fenced-block migration ---
//
// Per-workspace TOOLS.md and AGENTS.md sections are wrapped in versioned
// fence markers:
//
//   <!-- memclaw:tools v=<8-hex> -->
//   …rendered block…
//   <!-- /memclaw:tools -->
//
// The version is the first 8 hex chars of the SHA-256 of the block string.
// On subsequent runs, `spliceFencedBlock` decides what to do based on
// what it finds in the existing file:
//
//   1. Fence with matching version → no-op (idempotent).
//   2. Fence with different version → in-place replace, content outside
//      the fence preserved byte-for-byte. Lets us push education
//      updates to existing installs on heartbeat.
//   3. No fence, but a legacy heading (the pre-fence block layout from
//      v1.x) → splice from one preceding `---` rule (if any) through
//      the heading and on to the next H2 / EOF, then write the fenced
//      block in its place. A one-shot `<filename>.memclaw-bak` is
//      written before the splice so users who hand-edited inside our
//      block can recover.
//   4. No fence, no legacy heading → append at EOF (fresh install).
//
// This replaces the pre-fix presence-based idempotency
// (`includes("MemClaw")` / `includes("## Memory V2")`) which never
// updated stale content after a plugin upgrade.

function blockHash(content: string): string {
  return createHash("sha256").update(content).digest("hex").slice(0, 8);
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Find the line range covered by a legacy MemClaw section (pre-fence
 * format), or null if no MemClaw legacy heading is present.
 *
 * Uses **prefix matching** on the heading rather than exact-equality:
 * the plugin has emitted at least two heading forms over its history
 * (e.g. v0.98.5 wrote `## MemClaw — Long-Term Agent Memory (auto-added
 * by plugin)` for TOOLS.md; v1.x wrote `## MemClaw — Tools Available`).
 * Both share a stable prefix (`## MemClaw —`), so prefix matching
 * catches every historical form in one pass without us having to
 * enumerate them.
 *
 * To prevent false positives on user-authored headings that happen to
 * start with the same prefix, the splice range is additionally
 * required to contain a MemClaw token (default: `memclaw_`). If the
 * range doesn't contain it, the function returns null and the splice
 * is skipped.
 *
 * The range starts at one preceding `---` rule (if any blank-only lines
 * separate it from the heading) and ends at the next `## ` heading or
 * EOF — so splicing it out removes our block cleanly without touching
 * surrounding user content.
 */
export function findLegacyRange(
  content: string,
  legacyHeadingPrefix: string,
  contentMustInclude: string = "memclaw_",
): { start: number; end: number } | null {
  // Split on LF, not CRLF: under CRLF input each `lines[i]` carries a
  // trailing `\r` and `lines[i].length + 1` (LF) sums to the correct
  // offset in the original string regardless of line ending. The only
  // place CRLF leaks through is the prefix check below — strip a
  // trailing `\r` for that comparison so the offset math stays
  // consistent with the original (un-normalized) content.
  const lines = content.split("\n");
  const stripCR = (s: string): string =>
    s.length > 0 && s.charCodeAt(s.length - 1) === 13 ? s.slice(0, -1) : s;

  let h = -1;
  for (let i = 0; i < lines.length; i++) {
    if (stripCR(lines[i]).startsWith(legacyHeadingPrefix)) {
      h = i;
      break;
    }
  }
  if (h === -1) return null;

  // Walk back through blank lines; if the previous non-blank is `---`,
  // include it in the range. Otherwise start at the heading.
  let s = h;
  let probe = h - 1;
  while (probe >= 0 && lines[probe].trim() === "") probe--;
  if (probe >= 0 && lines[probe].trim() === "---") {
    s = probe;
  }

  // End: next H2 after the heading, or EOF.
  let e = lines.length;
  for (let i = h + 1; i < lines.length; i++) {
    if (lines[i].startsWith("## ")) {
      e = i;
      break;
    }
  }

  // Convert line indices to character offsets. When `e === lines.length`
  // and the source content has no trailing newline, the per-line `+1` (for
  // the LF that's no longer there) over-counts by one. `slice` would still
  // do the right thing, but the explicit clamp documents the intent.
  const startChar = lines.slice(0, s).reduce((acc, l) => acc + l.length + 1, 0);
  const endChar = Math.min(
    lines.slice(0, e).reduce((acc, l) => acc + l.length + 1, 0),
    content.length,
  );

  // Content-shape verification: the splice range MUST contain a
  // MemClaw token (default `memclaw_`). This guards against a user
  // heading that coincidentally starts with the same prefix but
  // contains no MemClaw content. If the slice doesn't smell like one
  // of our blocks, leave it alone.
  if (contentMustInclude) {
    const slice = content.slice(startChar, endChar);
    if (!slice.toLowerCase().includes(contentMustInclude.toLowerCase())) {
      return null;
    }
  }

  return { start: startChar, end: endChar };
}

interface SpliceResult {
  content: string;
  updated: boolean;
  backedUp: boolean;
}

export function spliceFencedBlock(
  existing: string,
  marker: "tools" | "agents",
  newBlock: string,
  legacyHeadingPrefix: string,
  options: { force?: boolean; backupPath?: string } = {},
): SpliceResult {
  const tag = `memclaw:${marker}`;
  const innerContent = newBlock.replace(/^\n+|\n+$/g, "");
  const version = blockHash(innerContent);
  const fenced = `<!-- ${tag} v=${version} -->\n${innerContent}\n<!-- /${tag} -->\n`;

  // Case 1 + 2: existing fenced block.
  const fenceRe = new RegExp(
    `<!--\\s*${escapeRegex(tag)}\\s+v=([a-f0-9]+)\\s*-->[\\s\\S]*?<!--\\s*/${escapeRegex(tag)}\\s*-->\\n?`,
  );
  const fenceMatch = existing.match(fenceRe);
  if (fenceMatch) {
    if (!options.force && fenceMatch[1] === version) {
      return { content: existing, updated: false, backedUp: false };
    }
    // Stale-version replace (or force-rewrite). Mirror the legacy-bridge
    // backup behaviour: a user may have hand-edited inside the fenced
    // block (e.g. added a personal comment); a version bump would
    // otherwise silently discard those edits. The one-shot
    // `<file>.memclaw-bak` preserves the pre-replace content for
    // recovery. Same one-shot semantics as case 3 below: if the
    // backup already exists, leave it alone.
    let backedUp = false;
    if (options.backupPath && !existsSync(options.backupPath)) {
      try {
        writeFileSync(options.backupPath, existing, "utf-8");
        backedUp = true;
      } catch (e: unknown) {
        logError(`Failed to write backup at ${options.backupPath}`, e);
      }
    }
    const start = fenceMatch.index!;
    const end = start + fenceMatch[0].length;
    return {
      content: existing.slice(0, start) + fenced + existing.slice(end),
      updated: true,
      backedUp,
    };
  }

  // Case 3: legacy heading present.
  const legacyRange = findLegacyRange(existing, legacyHeadingPrefix);
  if (legacyRange) {
    let backedUp = false;
    if (options.backupPath && !existsSync(options.backupPath)) {
      try {
        writeFileSync(options.backupPath, existing, "utf-8");
        backedUp = true;
      } catch (e: unknown) {
        logError(`Failed to write backup at ${options.backupPath}`, e);
      }
    }
    return {
      content:
        existing.slice(0, legacyRange.start) +
        fenced +
        existing.slice(legacyRange.end),
      updated: true,
      backedUp,
    };
  }

  // Case 4: append at EOF. Always leave a blank line between the
  // file's last content line and the fence opener so the appended
  // block is markdown-readable, regardless of how the existing file
  // ends.
  const sep =
    existing.length === 0
      ? ""
      : existing.endsWith("\n\n")
        ? ""
        : existing.endsWith("\n")
          ? "\n"
          : "\n\n";
  return {
    content: existing + sep + fenced,
    updated: true,
    backedUp: false,
  };
}

/**
 * Append MemClaw sections to TOOLS.md and AGENTS.md in each agent workspace.
 *
 * SKILL.md is no longer written here: it ships as a static file at
 * `<plugin-root>/skills/memclaw/SKILL.md` and is discovered by OpenClaw
 * via the `skills` field in `openclaw.plugin.json` — one copy per node,
 * auto-gated by plugin enablement.
 *
 * Called from both auto-education (first load) and the "educate" command.
 *
 * Idempotency is version-based via `spliceFencedBlock` (see above): a
 * workspace already carrying the current-version fenced block is
 * left untouched; a workspace with a stale-version fence or a legacy
 * pre-fence section is auto-migrated to the current content.
 *
 * `options.force=true` bypasses the version compare and rewrites the
 * fenced block even when it already matches.
 */
export function writeEducationFiles(
  toolsMdSection: string,
  agentsMdSection: string,
  agentIds?: string[],
  baseDir?: string,
  options: { force?: boolean } = {},
): { toolsUpdated: number; agentsUpdated: number } {
  const ocBase = baseDir || getOpenClawBaseDir();
  const wsList = discoverAgentWorkspaces(ocBase, agentIds);
  let toolsUpdated = 0;
  let agentsUpdated = 0;

  for (const { path: wsPath } of wsList) {
    try {
      const toolsPath = join(wsPath, "TOOLS.md");
      const existingTools = existsSync(toolsPath)
        ? readFileSync(toolsPath, "utf-8")
        : "";
      const toolsResult = spliceFencedBlock(
        existingTools,
        "tools",
        toolsMdSection,
        // Prefix, not exact match. Catches every legacy heading the
        // plugin has emitted: `## MemClaw — Tools Available` (1.x),
        // `## MemClaw — Long-Term Agent Memory (auto-added by plugin)`
        // (0.98.5), and any future variant that keeps the same
        // `## MemClaw —` lead.
        "## MemClaw —",
        {
          force: options.force,
          backupPath: toolsPath + ".memclaw-bak",
        },
      );
      if (toolsResult.updated) {
        writeFileSync(toolsPath, toolsResult.content, "utf-8");
        toolsUpdated++;
      }

      const agentsPath = join(wsPath, "AGENTS.md");
      const existingAgents = existsSync(agentsPath)
        ? readFileSync(agentsPath, "utf-8")
        : "";
      const agentsResult = spliceFencedBlock(
        existingAgents,
        "agents",
        agentsMdSection,
        // Prefix, not exact match. Catches `## Memory V2 — MemClaw
        // Protocol (mandatory)` (1.x), `## Memory V2 (auto-added by
        // MemClaw plugin — replaces any earlier memory section above)`
        // (0.98.5), and any future variant under the same `## Memory V2`
        // family. The content-shape check (`memclaw_` token) prevents
        // false positives on user prose.
        "## Memory V2",
        {
          force: options.force,
          backupPath: agentsPath + ".memclaw-bak",
        },
      );
      if (agentsResult.updated) {
        writeFileSync(agentsPath, agentsResult.content, "utf-8");
        agentsUpdated++;
      }

      // One-shot cleanup of the pre-C1 DEFAULT_EDUCATION paragraph the
      // plugin used to append to HEARTBEAT.md on first install.
      // Idempotent: a HEARTBEAT.md that doesn't contain the paragraph
      // (or doesn't exist) is a no-op. If stripping the paragraph
      // leaves the file empty (the first-install case where the file
      // was created solely to hold our paragraph), unlink rather than
      // writing a 0-byte file: educateAgents treats absent and empty
      // identically when the host later pushes a real prompt.
      const hbPath = join(wsPath, "HEARTBEAT.md");
      if (existsSync(hbPath)) {
        const existingHb = readFileSync(hbPath, "utf-8");
        const hbResult = cleanupStaleHeartbeatEducation(existingHb);
        if (hbResult.cleaned) {
          if (hbResult.content.length === 0) {
            unlinkSync(hbPath);
          } else {
            writeFileSync(hbPath, hbResult.content, "utf-8");
          }
        }
      }
    } catch (e: unknown) {
      logError(`writeEducationFiles failed for ${wsPath}`, e);
    }
  }

  // One-shot cleanup of phantom files written by the pre-fix discovery,
  // which mistakenly treated <baseDir>/workspaces/ (the plural parent
  // directory) as a single workspace. Idempotent.
  cleanupPhantomEducationFiles(ocBase);

  return { toolsUpdated, agentsUpdated };
}
