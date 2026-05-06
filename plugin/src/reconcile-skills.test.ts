/**
 * Tests for ``reconcileSkills`` — the Phase A plugin-side skill reconciler.
 *
 * Locked-in invariants:
 *
 *   1. **Bundled skill protected** — empty catalog must NEVER delete
 *      the bundled ``memclaw`` skill, even though it isn't a catalog
 *      row. Wiping it on every fresh-tenant heartbeat would be
 *      catastrophic — the skill is the agent's onboarding doc.
 *   2. **Cold start pulls everything** — fresh node with only the
 *      bundled ``memclaw`` skill must materialise every catalog skill
 *      on first heartbeat.
 *   3. **Convergence after offline period** — skills present in catalog
 *      but missing from disk get added; skills on disk but absent from
 *      catalog get removed (subject to invariant #1). Two changes in
 *      the same tick → both applied.
 *   4. **Idempotent** — re-running with no changes is a no-op (no
 *      writes, no deletes, no spurious mtime bumps).
 *   5. **Bad slug from server is rejected client-side** — defense in
 *      depth: even if the server validation regresses, an unsafe slug
 *      (path traversal etc.) must NOT land on disk.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

// Set env BEFORE importing reconcile-skills.js — module reads from
// process.env at import time via env.ts.
process.env.MEMCLAW_API_KEY = "mc_test_key_for_reconcile_tests";
process.env.MEMCLAW_API_URL = "http://localhost:8000";
process.env.MEMCLAW_TENANT_ID = "t_test";

// Redirect HOME so getPluginDir() returns a tmpdir instead of a real
// ~/.openclaw — keeps the test from touching the dev's plugin install.
const originalHome = process.env.HOME;
const tmpHome = mkdtempSync(join(tmpdir(), "reconcile-skills-test-home-"));
process.env.HOME = tmpHome;

const { reconcileSkills, PROTECTED_SKILLS } = await import("./reconcile-skills.js");

const SKILLS_ROOT = join(tmpHome, ".openclaw", "plugins", "memclaw", "skills");

let originalFetch: typeof fetch;
let mockCatalog: Array<{ doc_id: string; data: { content: string } }>;

function installMockFetch(): void {
  originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/api/v1/documents/query")) {
      return new Response(JSON.stringify({ documents: mockCatalog }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(`unexpected url: ${url}`, { status: 500 });
  }) as typeof fetch;
}

function restoreFetch(): void {
  globalThis.fetch = originalFetch;
}

function resetSkillsDir(): void {
  if (existsSync(SKILLS_ROOT)) {
    rmSync(SKILLS_ROOT, { recursive: true, force: true });
  }
}

function plantOnDisk(slug: string, content = "# bundled\n"): void {
  const dir = join(SKILLS_ROOT, slug);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "SKILL.md"), content, "utf-8");
}

function listSkillDirs(): string[] {
  if (!existsSync(SKILLS_ROOT)) return [];
  return readdirSync(SKILLS_ROOT).sort();
}

function readSkill(slug: string): string {
  return readFileSync(join(SKILLS_ROOT, slug, "SKILL.md"), "utf-8");
}

describe("reconcileSkills", () => {
  beforeEach(() => {
    resetSkillsDir();
    installMockFetch();
    mockCatalog = [];
  });

  afterEach(() => {
    restoreFetch();
  });

  test("invariant 1: bundled `memclaw` skill is never deleted (empty catalog)", async () => {
    plantOnDisk("memclaw", "# bundled onboarding skill — should survive\n");
    plantOnDisk("foo", "# orphan from a previous unshared skill\n");
    mockCatalog = []; // empty catalog

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw"]); // foo gone, memclaw stays
    assert.deepEqual(summary.removed, ["foo"]);
    assert.deepEqual(summary.protected, ["memclaw"]);
    // Bundled content must be untouched
    assert.match(readSkill("memclaw"), /should survive/);
  });

  test("PROTECTED_SKILLS is exported and contains memclaw", () => {
    assert.ok(PROTECTED_SKILLS.has("memclaw"));
  });

  test("invariant 2: cold start pulls every catalog skill", async () => {
    plantOnDisk("memclaw"); // only the bundled skill
    mockCatalog = [
      { doc_id: "git-rebase-safety", data: { content: "# rebase safely\n" } },
      { doc_id: "deploy-runbook",    data: { content: "# deploy steps\n" } },
      { doc_id: "incident-triage",   data: { content: "# triage\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), [
      "deploy-runbook", "git-rebase-safety", "incident-triage", "memclaw",
    ]);
    assert.deepEqual(summary.added.sort(), ["deploy-runbook", "git-rebase-safety", "incident-triage"]);
    assert.deepEqual(summary.removed, []);
    assert.equal(readSkill("git-rebase-safety"), "# rebase safely\n");
  });

  test("invariant 3: convergence — adds B, removes C, in one tick", async () => {
    plantOnDisk("memclaw");
    plantOnDisk("skill-a", "# A from catalog\n");
    plantOnDisk("skill-c", "# C — orphan\n");
    mockCatalog = [
      { doc_id: "skill-a", data: { content: "# A from catalog\n" } },
      { doc_id: "skill-b", data: { content: "# B — newly shared\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw", "skill-a", "skill-b"]);
    assert.deepEqual(summary.added, ["skill-b"]);
    assert.deepEqual(summary.removed, ["skill-c"]);
  });

  test("invariant 4: re-running with no changes is a no-op", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "skill-a", data: { content: "# A\n" } },
    ];

    const first = await reconcileSkills();
    const second = await reconcileSkills();

    assert.deepEqual(first.added, ["skill-a"]);
    assert.deepEqual(second.added, []);
    assert.deepEqual(second.removed, []);
    // The skill on disk hasn't been overwritten (same content → skipped)
    assert.equal(readSkill("skill-a"), "# A\n");
  });

  test("invariant 5: unsafe slug from catalog is skipped, never lands on disk", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "../etc/passwd", data: { content: "exploit\n" } },
      { doc_id: "Capitalized",   data: { content: "uppercase rejected\n" } },
      { doc_id: "valid-slug",    data: { content: "# valid\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw", "valid-slug"]);
    assert.deepEqual(summary.added, ["valid-slug"]);
    assert.equal(summary.skipped.length, 2);
    // No traversal artefact created
    assert.ok(!existsSync(join(tmpHome, "etc", "passwd")));
  });

  test("catalog returns missing/empty content → row skipped, others applied", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "no-content", data: {} as { content: string } }, // explicitly empty
      { doc_id: "good",       data: { content: "# good\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["good", "memclaw"]);
    assert.deepEqual(summary.added, ["good"]);
    assert.equal(summary.skipped.length, 1);
  });

  test("catalog query failure → fail open: existing skills preserved", async () => {
    plantOnDisk("memclaw");
    plantOnDisk("skill-a", "# from previous tick\n");
    // Replace fetch with a thrower
    globalThis.fetch = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;

    const summary = await reconcileSkills();

    // Disk untouched
    assert.deepEqual(listSkillDirs(), ["memclaw", "skill-a"]);
    assert.equal(summary.catalogCount, 0);
    assert.deepEqual(summary.added, []);
    assert.deepEqual(summary.removed, []);
  });

  test("content drift on disk → reconciler overwrites with catalog version", async () => {
    plantOnDisk("memclaw");
    plantOnDisk("skill-a", "# stale local edits\n");
    mockCatalog = [
      { doc_id: "skill-a", data: { content: "# canonical from catalog\n" } },
    ];

    await reconcileSkills();

    assert.equal(readSkill("skill-a"), "# canonical from catalog\n");
  });
});

// Restore HOME after the suite so subsequent test files (run in
// the same process under --test-isolation=none) see the real value.
process.on("exit", () => {
  process.env.HOME = originalHome;
  try {
    rmSync(tmpHome, { recursive: true, force: true });
  } catch {
    // best-effort cleanup
  }
});
