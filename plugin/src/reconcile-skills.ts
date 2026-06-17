/**
 * Plugin-side skill reconciler — Phase A of the skills-as-documents
 * migration.
 *
 * On every heartbeat, mirror the visible ``collection=skills`` catalog
 * onto ``<target>/<slug>/SKILL.md`` on local disk. Replaces the dropped
 * ``install_skill`` / ``uninstall_skill`` fleet commands (which were
 * Phase B's removed push-mode behaviour).
 *
 * Targets: by default the reconciler converges the plugin's own skills
 * dir (``getPluginDir()/skills``) in ``owned`` mode. Additional targets
 * can be configured via ``MEMCLAW_SKILL_TARGETS`` (see
 * {@link resolveSkillTargets}). Two modes:
 *   - ``owned`` ({@link reconcileOwnedDir}): the dir is fully
 *     MemClaw-managed; any on-disk skill not in the catalog is pruned
 *     (except {@link PROTECTED_SKILLS}).
 *   - ``additive`` ({@link reconcileAdditiveDir}): a shared/foreign dir.
 *     MemClaw only ever touches entries it wrote, tracked per-skill via
 *     the {@link OWNED_MARKER} sentinel — foreign skills are never
 *     overwritten (collisions are skipped) or removed.
 * With no config, the single default ``owned`` target makes behaviour
 * byte-identical to before targets were configurable.
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
 *   the catalog and is removed from disk on the next tick (owned mode).
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
import { join, resolve } from "path";

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

// Per-skill ownership marker for ``additive`` (shared/foreign) target
// dirs. MemClaw writes this sentinel inside every skill dir it creates
// there, and only ever updates/removes a ``<slug>`` that carries it — so
// a skill it doesn't own is never touched. The marker lives INSIDE the
// skill dir (``<dir>/<slug>/.memclaw-owned``); OpenClaw's loader reads
// only ``SKILL.md`` and halts recursion at a skill root, so the marker is
// invisible to it (verified against agent-core skills.ts / session.ts).
// Choosing a per-skill marker over a central manifest makes the safety
// property fail-safe: a missing marker means "leave it alone", never
// "delete it".
export const OWNED_MARKER = ".memclaw-owned";
const OWNED_MARKER_BODY =
  "This skill directory is managed by the MemClaw plugin reconciler.\n" +
  "Do not edit by hand — it is overwritten/removed to match the catalog.\n";

/** True if ``skillDir`` carries the MemClaw ownership marker. */
function isMemclawOwned(skillDir: string): boolean {
  return existsSync(join(skillDir, OWNED_MARKER));
}

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
  // Aggregated across all reconciled targets (deduplicated, sorted).
  installed: string[];
  added: string[];
  removed: string[];
  skipped: string[];   // catalog entries with bad shape (no doc_id / no content)
  // Slugs not written because an unowned dir already occupies the slot in
  // an ``additive`` target (a foreign skill we refuse to clobber). Distinct
  // from ``skipped`` so a caller can tell a catalog-shape error apart from a
  // naming conflict with a client-owned skill. Empty unless an ``additive``
  // target is configured.
  collisions: string[];
  protected: string[]; // catalog-absent but not deleted
}

/**
 * How aggressively the reconciler may prune a target dir.
 *
 * - ``owned``: the dir is fully MemClaw-managed — every on-disk entry
 *   not in the catalog is deleted (except {@link PROTECTED_SKILLS}).
 * - ``additive``: a shared/foreign dir — MemClaw must only ever touch
 *   entries it wrote, never prune others. Parsed but NOT yet acted on
 *   here (a later PR implements ownership-tracked additive reconcile);
 *   until then such targets are skipped with a warning.
 */
export type SkillTargetMode = "owned" | "additive";

export interface SkillTarget {
  dir: string;
  mode: SkillTargetMode;
}

/** The plugin's own skills dir — always reconciled in ``owned`` mode. */
function ownedSkillsDir(): string {
  return join(getPluginDir(), "skills");
}

/**
 * Resolve the target dirs to reconcile this tick.
 *
 * Always includes the plugin's owned dir (``owned`` mode). Additional
 * targets come from the ``MEMCLAW_SKILL_TARGETS`` env var — a JSON array
 * of ``{ dir, mode }``. Read at call time (``env.ts`` has already loaded
 * ``.env`` into ``process.env`` at import). The parse fails safe: invalid
 * JSON, a non-array value, or a malformed entry is logged and ignored so
 * a bad config can never crash the heartbeat or alter the owned dir. An
 * entry pointing at the owned dir is dropped (it's always present, in
 * ``owned`` mode).
 */
export function resolveSkillTargets(): SkillTarget[] {
  // resolve() so the dedup seed uses the same canonical representation as
  // the resolve()d entries below — otherwise a non-canonical owned path
  // (e.g. with ``..`` segments) would let an entry resolving to the same
  // dir slip past the seen-set and reconcile the owned dir twice.
  const ownedDir = resolve(ownedSkillsDir());
  const targets: SkillTarget[] = [{ dir: ownedDir, mode: "owned" }];

  const raw = process.env.MEMCLAW_SKILL_TARGETS;
  if (!raw || !raw.trim()) return targets;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e: unknown) {
    logError("resolveSkillTargets: MEMCLAW_SKILL_TARGETS is not valid JSON; ignoring", e);
    return targets;
  }
  if (!Array.isArray(parsed)) {
    console.warn("[memclaw] MEMCLAW_SKILL_TARGETS must be a JSON array; ignoring");
    return targets;
  }

  const seen = new Set<string>([ownedDir]);
  for (const entry of parsed) {
    if (!entry || typeof entry !== "object") {
      console.warn("[memclaw] MEMCLAW_SKILL_TARGETS: skipping non-object entry");
      continue;
    }
    const dir = (entry as { dir?: unknown }).dir;
    const mode = (entry as { mode?: unknown }).mode;
    if (typeof dir !== "string" || !dir.trim()) {
      console.warn("[memclaw] MEMCLAW_SKILL_TARGETS: entry missing string 'dir'; skipping");
      continue;
    }
    if (mode !== "owned" && mode !== "additive") {
      console.warn(`[memclaw] MEMCLAW_SKILL_TARGETS: entry ${dir} has invalid mode ${String(mode)}; skipping`);
      continue;
    }
    const normalized = resolve(dir);
    // Safety: owned-mode reconcile rmSync's orphans under the target, so a
    // too-shallow path (``/``, ``/tmp``) would be catastrophic if
    // misconfigured. Require at least two path segments.
    const parts = normalized.split("/").filter(Boolean);
    if (parts.length < 2) {
      console.warn(
        `[memclaw] MEMCLAW_SKILL_TARGETS: entry dir ${normalized} is too shallow; skipping`,
      );
      continue;
    }
    if (seen.has(normalized)) continue; // dedupe; owned dir already present
    seen.add(normalized);
    targets.push({ dir: normalized, mode });
  }
  return targets;
}

interface DirReconcileResult {
  added: string[];
  removed: string[];
  protected: string[];
  /** Confirmed-on-disk catalog skills for this dir, excluding PROTECTED. */
  installed: string[];
  /**
   * Desired skills NOT materialised because the slug is already occupied
   * by a foreign (non-MemClaw-owned) entry in an ``additive`` dir. Always
   * empty for ``owned`` dirs (which fully own their contents).
   */
  collisions: string[];
}

/**
 * Read the immediate subdirectory names (candidate skill slugs) of
 * ``root``. Non-directory entries are skipped so a stray file isn't
 * treated as a managed slug; a stat failure on one entry is non-fatal.
 * Returns ``null`` (and logs) if the directory itself can't be read, so
 * callers bail out rather than acting on an empty set.
 */
function readDirSlugs(root: string, caller: string): Set<string> | null {
  const slugs = new Set<string>();
  try {
    for (const name of readdirSync(root)) {
      try {
        if (statSync(join(root, name)).isDirectory()) slugs.add(name);
      } catch {
        // stat failure on one entry is non-fatal for the rest
      }
    }
  } catch (e: unknown) {
    logError(`${caller}: failed to read skills directory`, e);
    return null;
  }
  return slugs;
}

/**
 * Reconcile ONE ``owned`` target dir against the desired catalog set:
 * prune orphans (anything on disk not in ``desired``, except
 * {@link PROTECTED_SKILLS}), then write/update the desired skills.
 * Returns this dir's contribution to the summary; never throws.
 */
function reconcileOwnedDir(
  skillsRoot: string,
  desired: Map<string, string>,
): DirReconcileResult {
  const result: DirReconcileResult = {
    added: [],
    removed: [],
    protected: [],
    installed: [],
    collisions: [],
  };

  // Read disk. Skip non-directories so a stray file in the target dir
  // doesn't get treated as a managed slug.
  if (!existsSync(skillsRoot)) {
    mkdirSync(skillsRoot, { recursive: true });
  }
  const onDisk = readDirSlugs(skillsRoot, "reconcileOwnedDir");
  if (!onDisk) return result;

  // Apply diff. Order: removals first, then writes — so a rename
  // (slug A → slug B) lands cleanly even if the operator does both in
  // the same heartbeat window.
  for (const slug of onDisk) {
    if (desired.has(slug)) continue;
    if (PROTECTED_SKILLS.has(slug)) {
      result.protected.push(slug);
      continue;
    }
    try {
      rmSync(join(skillsRoot, slug), { recursive: true, force: true });
      result.removed.push(slug);
      console.log(`[memclaw] Reconciler removed orphan skill: ${slug}`);
    } catch (e: unknown) {
      logError(`reconcileOwnedDir: rm failed for ${slug}`, e);
    }
  }

  // Track CONFIRMED on-disk state for ``installed``. Seed with skills
  // that are BOTH already on disk AND catalog-active this tick
  // (``onDisk ∩ desired``); the write loop then adds each fresh
  // successful write. Intersecting with ``desired`` — rather than
  // ``onDisk`` minus successful removals — is what makes this correct in
  // every case:
  //   • same-content skill skipped by the no-op-match branch below:
  //     in onDisk ∩ desired → kept (it's genuinely present);
  //   • new install: not on disk → added only on a successful write;
  //   • cleanly removed orphan: not in desired → excluded;
  //   • FAILED removal of a deactivated skill (rmSync threw, so it's
  //     absent from removed but still on disk): not in desired →
  //     excluded, so a stale skill is never reported as installed.
  const desiredSlugs = new Set(desired.keys());
  const installedSet = new Set([...onDisk].filter((s) => desiredSlugs.has(s)));
  for (const [slug, content] of desired) {
    const dir = join(skillsRoot, slug);
    const target = join(dir, "SKILL.md");
    // Skip writes when the on-disk content already matches the catalog's
    // content — keeps mtime stable and avoids spamming OpenClaw's
    // skill-watch reload path on every heartbeat.
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
      result.added.push(slug);
      console.log(
        `[memclaw] Reconciler ${onDisk.has(slug) ? "updated" : "pulled"} skill: ${slug}`,
      );
    } catch (e: unknown) {
      logError(`reconcileOwnedDir: write failed for ${slug}`, e);
    }
  }

  result.installed = [...installedSet].filter((s) => !PROTECTED_SKILLS.has(s));
  return result;
}

/**
 * Reconcile ONE ``additive`` (shared / foreign) target dir.
 *
 * Unlike {@link reconcileOwnedDir}, MemClaw does NOT own this dir, so it
 * must never touch an entry it didn't write. Safety is enforced by the
 * per-skill {@link OWNED_MARKER}:
 *
 *  - **write**: a desired slug is written only when its dir is absent
 *    (new → stamp the marker) or already MemClaw-owned (update in place).
 *    A slug occupied by an UNOWNED dir is a collision → skipped, never
 *    overwritten.
 *  - **remove**: an on-disk slug not in the catalog is removed only when
 *    it carries the marker; unowned (foreign) entries are left untouched.
 *
 * Consequence: an empty catalog (or a misconfigured tenant returning an
 * empty installable set) prunes only MemClaw-owned entries here —
 * foreign skills survive. Never throws.
 */
function reconcileAdditiveDir(
  skillsRoot: string,
  desired: Map<string, string>,
): DirReconcileResult {
  const result: DirReconcileResult = {
    added: [],
    removed: [],
    protected: [],
    installed: [],
    collisions: [],
  };

  if (!existsSync(skillsRoot)) {
    mkdirSync(skillsRoot, { recursive: true });
  }
  const onDisk = readDirSlugs(skillsRoot, "reconcileAdditiveDir");
  if (!onDisk) return result;

  // Removals first — but ONLY for MemClaw-owned (marker-bearing) orphans.
  // Anything without the marker is foreign and is never touched.
  for (const slug of onDisk) {
    if (desired.has(slug)) continue;
    // Ownership gates everything in an additive dir: a foreign slug is left
    // alone even if its name collides with a PROTECTED one. A foreign
    // "memclaw" dir (no marker) is the client's, not ours — ignore it
    // rather than misreport it as a MemClaw-protected skill. An OWNED
    // protected dir still survives via the protected check below.
    if (!isMemclawOwned(join(skillsRoot, slug))) continue; // foreign — leave alone
    if (PROTECTED_SKILLS.has(slug)) {
      result.protected.push(slug);
      continue;
    }
    try {
      rmSync(join(skillsRoot, slug), { recursive: true, force: true });
      result.removed.push(slug);
      console.log(`[memclaw] Reconciler (additive) removed owned orphan: ${slug}`);
    } catch (e: unknown) {
      logError(`reconcileAdditiveDir: rm failed for ${slug}`, e);
    }
  }

  // Track CONFIRMED on-disk state for ``installed``. Pre-seed with skills
  // already on disk, MemClaw-owned, AND catalog-active this tick — mirrors
  // reconcileOwnedDir so a transient read/write I/O failure doesn't drop a
  // physically-present skill from the installed report. (Unlike the owned
  // dir we additionally require the ownership marker — a foreign occupant
  // of a desired slug is never "ours" and is reported as a collision.)
  const installedSet = new Set<string>(
    [...onDisk].filter(
      (s) => desired.has(s) && isMemclawOwned(join(skillsRoot, s)),
    ),
  );

  // Writes — only into absent or already-owned slots. A foreign occupant
  // of the same slug is a collision: skip it, never clobber.
  for (const [slug, content] of desired) {
    const skillDir = join(skillsRoot, slug);
    // Re-stat live, not from the start-of-function ``onDisk`` snapshot: a
    // dir created OR removed between the readdir and now would otherwise be
    // misclassified (a fresh foreign dir slipping past the collision guard,
    // or a since-deleted owned dir wrongly read as a foreign collision).
    // The ``existsSync`` re-stat narrows the TOCTOU window; the
    // non-recursive ``mkdirSync`` below provides the actual atomic guard
    // via EEXIST.
    const dirExistsNow = existsSync(skillDir);
    if (dirExistsNow && !isMemclawOwned(skillDir)) {
      result.collisions.push(slug);
      console.warn(
        `[memclaw] additive: ${slug} is occupied by an unowned skill in ` +
          `${skillsRoot}; skipping (collision)`,
      );
      continue;
    }
    const target = join(skillDir, "SKILL.md");
    // Owned + unchanged content → already installed; skip the rewrite.
    if (dirExistsNow) {
      try {
        if (existsSync(target) && readFileSync(target, "utf-8") === content) {
          installedSet.add(slug);
          continue;
        }
      } catch {
        // Read failure → fall through and overwrite
      }
    }
    try {
      // Atomic create-or-detect: a non-recursive mkdir throws EEXIST if the
      // dir appeared after our existsSync check, closing the TOCTOU window
      // that a recursive (idempotent) mkdir would leave open.
      let isNew = false;
      try {
        mkdirSync(skillDir);
        isNew = true;
      } catch (mkdirErr: unknown) {
        if ((mkdirErr as NodeJS.ErrnoException).code !== "EEXIST") throw mkdirErr;
        // Dir raced into existence after our existsSync check — re-verify
        // ownership before touching it; a foreign winner is a collision.
        if (!isMemclawOwned(skillDir)) {
          result.collisions.push(slug);
          console.warn(
            `[memclaw] additive: ${slug} collision (post-existsSync race in ${skillsRoot}); skipping`,
          );
          continue;
        }
      }
      // Stamp ownership on a genuinely new dir. For an existing dir we only
      // reach here after the ownership check passed, so the marker should
      // already be present — but re-stamp defensively if it vanished
      // between that check and now (a marker IS the ownership signal, and a
      // dir missing it at guard time is treated as a foreign collision).
      if (isNew || !existsSync(join(skillDir, OWNED_MARKER))) {
        writeFileSync(join(skillDir, OWNED_MARKER), OWNED_MARKER_BODY, "utf-8");
      }
      writeFileSync(target, content, "utf-8");
      installedSet.add(slug);
      result.added.push(slug);
      console.log(
        `[memclaw] additive: ${isNew ? "pulled" : "updated"} skill ${slug} in ${skillsRoot}`,
      );
    } catch (e: unknown) {
      logError(`reconcileAdditiveDir: write failed for ${slug}`, e);
    }
  }

  result.installed = [...installedSet].filter((s) => !PROTECTED_SKILLS.has(s));
  return result;
}

/**
 * Mirror the catalog onto the configured target dirs. Returns a summary
 * for tests / logging; never throws.
 */
export async function reconcileSkills(): Promise<ReconcileSummary> {
  const summary: ReconcileSummary = {
    catalogCount: 0,
    installed: [],
    added: [],
    removed: [],
    skipped: [],
    collisions: [],
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

  // 2. Build the desired state from the catalog (target-independent).
  //    Skip rows missing doc_id or content — they can't be materialised.
  //    Slug validation (filesystem-safe) was enforced server-side by the
  //    Phase B ``memclaw_doc op=write collection=skills`` rule, so every
  //    doc_id we see here should already be safe — but defense in depth:
  //    re-validate before touching the filesystem.
  //
  //    OpenClaw's skill loader rejects any SKILL.md without YAML
  //    frontmatter declaring ``name`` and ``description`` (it returns
  //    null in ``loadSingleSkillDirectory`` when either is missing,
  //    silently filtering the skill out of the agent's tool palette).
  //    Skills uploaded via ``memclaw_doc op=write collection=skills``
  //    typically supply ``data.{name, description, content}`` as separate
  //    fields with the content being plain markdown — so the reconciler
  //    synthesises frontmatter from ``data.name`` and ``data.description``
  //    before writing, unless the content already starts with a ``---``
  //    fence (in which case the author's own frontmatter is preserved).
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

  // 3. Reconcile each configured target. Default is a single ``owned``
  //    target (the plugin's skills dir) → behaviour identical to before
  //    targets were configurable. ``owned`` dirs are fully managed
  //    (destructive prune); ``additive`` dirs are shared/foreign and are
  //    reconciled non-destructively via the ownership marker.
  const installedAll: string[] = [];
  const addedAll: string[] = [];
  const removedAll: string[] = [];
  const protectedAll: string[] = [];
  const collisionsAll: string[] = [];
  for (const target of resolveSkillTargets()) {
    const dirResult =
      target.mode === "additive"
        ? reconcileAdditiveDir(target.dir, desired)
        : reconcileOwnedDir(target.dir, desired);
    addedAll.push(...dirResult.added);
    removedAll.push(...dirResult.removed);
    protectedAll.push(...dirResult.protected);
    installedAll.push(...dirResult.installed);
    collisionsAll.push(...dirResult.collisions);
  }

  // Aggregate across targets at slug granularity — a skill present in
  // more than one target is one slug, not N — deduped + sorted for a
  // stable heartbeat payload. ``installed`` excludes the bundled
  // ``memclaw`` skill (filtered per dir). For the default single target
  // this is identical to the un-aggregated per-dir lists.
  summary.added = [...new Set(addedAll)].sort();
  summary.removed = [...new Set(removedAll)].sort();
  summary.protected = [...new Set(protectedAll)].sort();
  summary.installed = [...new Set(installedAll)].sort();
  // ``skipped`` stays catalog-shape errors only (populated above during
  // ``desired`` construction); additive-dir collisions are reported
  // separately so the two failure modes don't get conflated.
  summary.skipped = [...new Set(summary.skipped)].sort();
  summary.collisions = [...new Set(collisionsAll)].sort();

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
