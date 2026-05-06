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
 * - **Pull-everything-visible** (locked in 2026-05-05): tenant + fleet
 *   visibility scoping happens at the catalog query layer, so the
 *   reconciler doesn't need its own opt-in flag. Skills the agent's
 *   fleet can see → on disk; can't see → not on disk.
 *
 * Failure mode: fail open. If the catalog query throws (network,
 * server, schema), the reconciler logs and returns; existing on-disk
 * skills are preserved untouched. The heartbeat loop continues.
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

interface ReconcileSummary {
  catalogCount: number;
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

  // 1. Fetch the catalog. Visibility filtering (fleet_id) happens
  //    server-side; we just pass our local fleet binding through.
  let catalog: CatalogDoc[];
  try {
    const resp = (await apiCall(
      "POST",
      "/documents/query",
      {
        tenant_id: MEMCLAW_TENANT_ID,
        collection: "skills",
        fleet_id: MEMCLAW_FLEET_ID || undefined,
        where: {},
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
  const desired = new Map<string, string>();
  for (const doc of catalog) {
    const slug = typeof doc.doc_id === "string" ? doc.doc_id : "";
    const content =
      doc.data && typeof doc.data["content"] === "string"
        ? (doc.data["content"] as string)
        : "";
    if (!slug || !isSafeSlug(slug) || !content) {
      summary.skipped.push(slug || "<missing>");
      continue;
    }
    desired.set(slug, content);
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
      summary.added.push(slug);
      console.log(
        `[memclaw] Reconciler ${onDisk.has(slug) ? "updated" : "pulled"} skill: ${slug}`,
      );
    } catch (e: unknown) {
      logError(`reconcileSkills: write failed for ${slug}`, e);
    }
  }

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
