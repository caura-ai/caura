/**
 * Plugin-side skill reconciler — Phase A of the skills-as-documents
 * migration.
 *
 * On every heartbeat, mirror the visible ``collection=skills`` catalog
 * onto ``plugin/skills/<slug>/SKILL.md`` on local disk. Replaces the
 * dropped ``install_skill`` / ``uninstall_skill`` fleet commands (which
 * were Phase B's removed push-mode behaviour).
 *
 * Properties:
 *
 * - **Declarative**: the catalog row IS the source of truth; the
 *   reconciler only converges the on-disk view.
 * - **Self-healing**: missed heartbeats catch up on the next tick.
 *   No queue, no "lost install command" failure mode.
 * - **Idempotent**: re-running is a no-op when disk already matches.
 * - **Server-gated catalog**: the reconciler pulls from
 *   ``/skills/installable``, which applies ALL policy server-side —
 *   tenant + fleet visibility AND (for Skill-Factory-opted-in tenants)
 *   the active-only lifecycle gate. So the reconciler carries no opt-in
 *   flag and no status filter: skills the server returns → on disk;
 *   anything it withholds (a sibling fleet's skill, or a candidate /
 *   staged / quarantined skill on an opted-in tenant) → never on disk.
 *   This is the SAME gate the MCP pull surface enforces (PR #315), so
 *   push (this reconciler) and pull (memclaw_doc) agree on what an agent
 *   may see. A skill flipping active→rejected/quarantined drops out of
 *   the catalog and is removed from disk on the next tick (step 4).
 *
 * Failure mode: fail open. If the catalog query throws or the server
 * returns non-2xx (network, server, schema, or the fail-closed 503 the
 * install surface raises during a settings outage), the reconciler logs
 * and returns; existing on-disk skills are preserved untouched and
 * nothing new is written — so an outage can never push a non-active
 * skill to disk. The heartbeat loop continues.
 */

import {
  existsSync, mkdirSync, readdirSync, readFileSync,
  rmSync, statSync, writeFileSync,
} from "fs";
import { join } from "path";

import { apiCall } from "./transport.js";
import { MEMCLAW_TENANT_ID, MEMCLAW_FLEET_ID } from "./env.js";
import { getPluginDir } from "./config.js";
import { logError } from "./logger.js";

/**
 * Bundled skills shipped with the plugin install. Never deleted by
 * reconciliation, even when the catalog returns no rows (a fresh
 * tenant or a fleet with zero shared skills should NOT wipe the
 * agent's onboarding skill).
 */
export const PROTECTED_SKILLS: ReadonlySet<string> = new Set(["memclaw"]);

interface CatalogDoc {
  doc_id?: string;
  data?: Record<string, unknown>;
}

export interface ReconcileSummary {
  catalogCount: number;
  // The converged catalog-active skills now materialised on disk (the
  // desired set this tick). Unlike ``added``/``removed`` — which are
  // per-tick DELTAS, empty once a node is steady-state — ``installed`` is
  // the standing truth: "these active skills are present on this node
  // right now." Surfaced on the heartbeat so an operator can confirm an
  // approved/active skill actually landed on the fleet. Excludes the
  // bundled ``memclaw`` onboarding skill (reported via ``protected``).
  installed: string[];
  added: string[];
  removed: string[];
  skipped: string[];   // catalog entries with bad shape (no doc_id / no content)
  protected: string[]; // catalog-absent but not deleted
}

/**
 * Mirror the catalog onto ``plugin/skills/``. Returns a summary for
 * tests / logging; never throws.
 */
export async function reconcileSkills(): Promise<ReconcileSummary> {
  const summary: ReconcileSummary = {
    catalogCount: 0,
    installed: [],
    added: [],
    removed: [],
    skipped: [],
    protected: [],
  };

  if (!MEMCLAW_TENANT_ID) {
    // No tenant resolved — heartbeat already short-circuits in this
    // case, but the reconciler is called independently in tests.
    return summary;
  }

  // 1. Fetch the installable catalog. ALL policy — tenant + fleet
  //    visibility AND the active-only Skill-Factory gate (for opted-in
  //    tenants) — is applied server-side by ``/skills/installable``; we
  //    just pass our tenant + fleet binding through. The endpoint
  //    cannot be widened by the client (no caller ``where``), so a node
  //    can never pull a non-active skill.
  let catalog: CatalogDoc[];
  try {
    const resp = (await apiCall(
      "POST",
      "/skills/installable",
      {
        tenant_id: MEMCLAW_TENANT_ID,
        fleet_id: MEMCLAW_FLEET_ID || undefined,
        limit: 1000,
      },
    )) as { documents?: CatalogDoc[] } | CatalogDoc[];
    catalog = Array.isArray(resp)
      ? resp
      : Array.isArray(resp?.documents)
        ? resp.documents
        : [];
  } catch (e: unknown) {
    logError("reconcileSkills: catalog query failed", e);
    return summary;
  }
  summary.catalogCount = catalog.length;

  // 2. Build the desired state from the catalog. Skip rows missing
  //    doc_id or content — they can't be materialised. Slug
  //    validation (filesystem-safe) was enforced server-side by the
  //    Phase B ``memclaw_doc op=write collection=skills`` rule, so
  //    every doc_id we see here should already be safe — but defense
  //    in depth: re-validate before touching the filesystem.
  //
  //    OpenClaw's skill loader rejects any SKILL.md without YAML
  //    frontmatter declaring ``name`` and ``description`` (it returns
  //    null in ``loadSingleSkillDirectory`` when either is missing,
  //    silently filtering the skill out of the agent's tool palette).
  //    Skills uploaded via ``memclaw_doc op=write collection=skills``
  //    typically supply ``data.{name, description, content}`` as
  //    separate fields with the content being plain markdown — so the
  //    reconciler synthesises frontmatter from ``data.name`` and
  //    ``data.description`` before writing, unless the content already
  //    starts with a ``---`` fence (in which case the author's own
  //    frontmatter is preserved).
  const desired = new Map<string, string>();
  for (const doc of catalog) {
    const slug = typeof doc.doc_id === "string" ? doc.doc_id : "";
    const data = doc.data ?? {};
    const rawContent =
      typeof data["content"] === "string" ? (data["content"] as string) : "";
    const description =
      typeof data["description"] === "string"
        ? (data["description"] as string).trim()
        : "";
    const name =
      typeof data["name"] === "string" && (data["name"] as string).trim()
        ? (data["name"] as string).trim()
        : slug;
    if (!slug || !isSafeSlug(slug) || !rawContent || !description) {
      summary.skipped.push(slug || "<missing>");
      continue;
    }
    desired.set(slug, ensureFrontmatter(rawContent, name, description));
  }

  // 3. Read disk. Skip non-directories so a stray file in
  //    plugin/skills/ doesn't get treated as a managed slug.
  const skillsRoot = join(getPluginDir(), "skills");
  if (!existsSync(skillsRoot)) {
    mkdirSync(skillsRoot, { recursive: true });
  }
  const onDisk = new Set<string>();
  try {
    for (const name of readdirSync(skillsRoot)) {
      try {
        if (statSync(join(skillsRoot, name)).isDirectory()) {
          onDisk.add(name);
        }
      } catch {
        // stat failure on one entry is non-fatal for the rest
      }
    }
  } catch (e: unknown) {
    logError("reconcileSkills: failed to read skills directory", e);
    return summary;
  }

  // 4. Apply diff. Order: removals first, then writes — so a rename
  //    (slug A → slug B) lands cleanly even if the operator does both
  //    in the same heartbeat window.
  for (const slug of onDisk) {
    if (desired.has(slug)) continue;
    if (PROTECTED_SKILLS.has(slug)) {
      summary.protected.push(slug);
      continue;
    }
    try {
      rmSync(join(skillsRoot, slug), { recursive: true, force: true });
      summary.removed.push(slug);
      console.log(`[memclaw] Reconciler removed orphan skill: ${slug}`);
    } catch (e: unknown) {
      logError(`reconcileSkills: rm failed for ${slug}`, e);
    }
  }

  // Track CONFIRMED on-disk state for ``summary.installed``. Seed with
  // skills that are BOTH already on disk AND catalog-active this tick
  // (``onDisk ∩ desired``); the write loop then adds each fresh
  // successful write. Intersecting with ``desired`` — rather than
  // ``onDisk`` minus successful removals — is what makes this correct in
  // every case:
  //   • same-content skill skipped by the no-op-match branch below:
  //     in onDisk ∩ desired → kept (it's genuinely present);
  //   • new install: not on disk → added only on a successful write;
  //   • cleanly removed orphan: not in desired → excluded;
  //   • FAILED removal of a deactivated skill (rmSync threw, so it's
  //     absent from summary.removed but still on disk): not in desired
  //     → excluded, so a stale skill is never reported as installed.
  const desiredSlugs = new Set(desired.keys());
  const installedSet = new Set([...onDisk].filter((s) => desiredSlugs.has(s)));
  for (const [slug, content] of desired) {
    const dir = join(skillsRoot, slug);
    const target = join(dir, "SKILL.md");
    // Skip writes when the on-disk content already matches the
    // catalog's content — keeps mtime stable and avoids spamming
    // OpenClaw's skill-watch reload path on every heartbeat.
    if (existsSync(target)) {
      try {
        if (readFileSync(target, "utf-8") === content) continue;
      } catch {
        // Read failure → fall through and overwrite
      }
    }
    try {
      mkdirSync(dir, { recursive: true });
      writeFileSync(target, content, "utf-8");
      installedSet.add(slug);
      summary.added.push(slug);
      console.log(
        `[memclaw] Reconciler ${onDisk.has(slug) ? "updated" : "pulled"} skill: ${slug}`,
      );
    } catch (e: unknown) {
      logError(`reconcileSkills: write failed for ${slug}`, e);
    }
  }

  // The standing on-disk truth: skills CONFIRMED present on disk this
  // tick (sorted for a stable heartbeat payload / diff), excluding the
  // bundled ``memclaw`` onboarding skill (reported via ``protected``).
  // Built from confirmed writes + already-present survivors, NOT from
  // ``desired`` — so a skill whose write failed above is correctly
  // absent. Reported even when ``added``/``removed`` are empty (steady
  // state) so an operator always sees what's live, not just what changed.
  summary.installed = [...installedSet]
    .filter((s) => !PROTECTED_SKILLS.has(s))
    .sort();

  return summary;
}

// Mirrors ``core_api.routes.documents._SKILL_SLUG_RE`` /
// ``mcp_server._SKILL_SLUG_RE``. Defense in depth — server already
// validates this on upsert, but the reconciler interpolates the slug
// into a filesystem path so a regression on either side shouldn't be
// able to land an unsafe directory name on disk.
const SAFE_SLUG_RE = /^[a-z0-9][a-z0-9._-]{0,99}$/;

function isSafeSlug(s: string): boolean {
  return SAFE_SLUG_RE.test(s);
}

const FRONTMATTER_FENCE_RE = /^---\r?\n/;

/**
 * Synthesise YAML frontmatter from the catalog row when the body
 * doesn't already include it. OpenClaw's skill loader silently filters
 * any SKILL.md whose frontmatter is missing ``name`` or ``description``
 * (see ``plugin-sdk/src/agents/skills/...loadSingleSkillDirectory``),
 * so a reconciled skill without frontmatter would land on disk yet
 * never appear in the agent's tool palette.
 *
 * Authors who upload skills with their own frontmatter in
 * ``data.content`` get pass-through (we detect the leading ``---``
 * fence and don't touch the body). Authors who upload plain markdown
 * (the common case for ``memclaw_doc op=write collection=skills``) get
 * frontmatter prepended from ``data.name`` and ``data.description``.
 *
 * Description is YAML-escaped — wrapped in double quotes with embedded
 * quotes/backslashes escaped — so a multi-word description with
 * punctuation can't trip the YAML parser.
 */
function ensureFrontmatter(
  rawContent: string,
  name: string,
  description: string,
): string {
  if (FRONTMATTER_FENCE_RE.test(rawContent)) return rawContent;
  const escapedDescription = description
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"');
  // Single-line YAML strings — safe across the slug + description
  // shapes the server already enforces (description ≤ 500 chars per
  // skill_service validation, no newlines accepted).
  const fm =
    "---\n" +
    `name: ${name}\n` +
    `description: "${escapedDescription}"\n` +
    "---\n\n";
  return fm + rawContent;
}
