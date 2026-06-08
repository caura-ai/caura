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
type MockCatalogEntry = {
  doc_id: string;
  data: { name?: string; description?: string; content: string };
};
let mockCatalog: MockCatalogEntry[];

/**
 * Wrap a content body with the same frontmatter the reconciler would
 * synthesise. Used by tests that need disk state to *exactly match*
 * what the reconciler will write (so convergence tests don't see a
 * spurious update on every tick).
 */
function withSynthFrontmatter(name: string, description: string, body: string): string {
  return `---\nname: ${name}\ndescription: "${description}"\n---\n\n${body}`;
}

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
      { doc_id: "git-rebase-safety", data: { name: "git-rebase-safety", description: "rebase steps",   content: "# rebase safely\n" } },
      { doc_id: "deploy-runbook",    data: { name: "deploy-runbook",    description: "deploy steps",   content: "# deploy steps\n" } },
      { doc_id: "incident-triage",   data: { name: "incident-triage",   description: "triage steps",   content: "# triage\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), [
      "deploy-runbook", "git-rebase-safety", "incident-triage", "memclaw",
    ]);
    assert.deepEqual(summary.added.sort(), ["deploy-runbook", "git-rebase-safety", "incident-triage"]);
    assert.deepEqual(summary.removed, []);
    // Frontmatter synthesised from data.{name, description}; body preserved.
    const written = readSkill("git-rebase-safety");
    assert.match(written, /^---\nname: git-rebase-safety\ndescription: "rebase steps"\n---\n\n# rebase safely\n$/);
  });

  test("invariant 3: convergence — adds B, removes C, in one tick", async () => {
    plantOnDisk("memclaw");
    // Plant skill-a with the EXACT content the reconciler would write,
    // so the no-op-on-match path stays quiet for it.
    plantOnDisk("skill-a", withSynthFrontmatter("skill-a", "alpha", "# A from catalog\n"));
    plantOnDisk("skill-c", "# C — orphan\n");
    mockCatalog = [
      { doc_id: "skill-a", data: { name: "skill-a", description: "alpha", content: "# A from catalog\n" } },
      { doc_id: "skill-b", data: { name: "skill-b", description: "bravo — newly shared", content: "# B — newly shared\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw", "skill-a", "skill-b"]);
    assert.deepEqual(summary.added, ["skill-b"]);
    assert.deepEqual(summary.removed, ["skill-c"]);
  });

  test("invariant 4: re-running with no changes is a no-op", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "skill-a", data: { name: "skill-a", description: "alpha", content: "# A\n" } },
    ];

    const first = await reconcileSkills();
    const second = await reconcileSkills();

    assert.deepEqual(first.added, ["skill-a"]);
    assert.deepEqual(second.added, []);
    assert.deepEqual(second.removed, []);
    // The skill on disk hasn't been overwritten (same content → skipped)
    assert.equal(readSkill("skill-a"), withSynthFrontmatter("skill-a", "alpha", "# A\n"));
  });

  test("invariant 5: unsafe slug from catalog is skipped, never lands on disk", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "../etc/passwd", data: { name: "x", description: "exploit",     content: "exploit\n" } },
      { doc_id: "Capitalized",   data: { name: "x", description: "uppercase",   content: "rejected\n" } },
      { doc_id: "valid-slug",    data: { name: "valid-slug", description: "ok", content: "# valid\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw", "valid-slug"]);
    assert.deepEqual(summary.added, ["valid-slug"]);
    assert.equal(summary.skipped.length, 2);
    // No traversal artefact created
    assert.ok(!existsSync(join(tmpHome, "etc", "passwd")));
  });

  test("catalog returns missing content/description → row skipped, others applied", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "no-content",     data: { name: "x", description: "ok" } as MockCatalogEntry["data"] },
      { doc_id: "no-description", data: { name: "x",                       content: "# body\n" } as MockCatalogEntry["data"] },
      { doc_id: "good",           data: { name: "good", description: "ok", content: "# good\n" } },
    ];

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["good", "memclaw"]);
    assert.deepEqual(summary.added, ["good"]);
    assert.equal(summary.skipped.length, 2);
  });

  test("frontmatter synthesis: plain markdown gets name+description prepended for OpenClaw discovery", async () => {
    plantOnDisk("memclaw");
    mockCatalog = [
      {
        doc_id: "git-rebase-safety",
        data: {
          name: "git-rebase-safety",
          description: "Safely rebase a feature branch — quotes \"and\" backslashes \\ get escaped",
          content: "# Body\n\nStep 1.\n",
        },
      },
    ];

    await reconcileSkills();

    const written = readSkill("git-rebase-safety");
    // YAML frontmatter present; description double-quoted with escapes intact.
    assert.match(
      written,
      /^---\nname: git-rebase-safety\ndescription: "Safely rebase a feature branch — quotes \\"and\\" backslashes \\\\ get escaped"\n---\n\n/,
    );
    // Original body preserved after the fence
    assert.ok(written.endsWith("# Body\n\nStep 1.\n"));
  });

  test("frontmatter passthrough: skill content that already starts with --- is left untouched", async () => {
    plantOnDisk("memclaw");
    const authorContent =
      "---\nname: my-skill\ndescription: hand-authored\nuser-invocable: true\n---\n\n# Body\n";
    mockCatalog = [
      {
        doc_id: "authored",
        data: {
          name: "should-be-ignored",
          description: "should-be-ignored",
          content: authorContent,
        },
      },
    ];

    await reconcileSkills();

    // Author's frontmatter wins — reconciler doesn't double-prepend.
    assert.equal(readSkill("authored"), authorContent);
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
      { doc_id: "skill-a", data: { name: "skill-a", description: "alpha", content: "# canonical from catalog\n" } },
    ];

    await reconcileSkills();

    assert.equal(readSkill("skill-a"), withSynthFrontmatter("skill-a", "alpha", "# canonical from catalog\n"));
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
