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

const { reconcileSkills, PROTECTED_SKILLS, resolveSkillTargets } = await import("./reconcile-skills.js");

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
    // The reconciler now pulls from the server-gated install surface,
    // which applies the active-only + opt-in filter server-side. The
    // mock returns whatever ``mockCatalog`` holds — i.e. only what the
    // server would have already deemed installable.
    if (url.endsWith("/api/v1/skills/installable")) {
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
    // No catalog-active skills → nothing installed (the bundled memclaw
    // is protected, not "installed" from the catalog).
    assert.deepEqual(summary.installed, []);
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
    // ``installed`` = the converged catalog-active set on disk (sorted),
    // excluding the bundled ``memclaw`` skill.
    assert.deepEqual(summary.installed, [
      "deploy-runbook", "git-rebase-safety", "incident-triage",
    ]);
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
    // The standing-truth value: even on the steady-state tick (no
    // deltas), ``installed`` still reports what's live — this is exactly
    // why ``installed`` exists rather than reading the empty ``added``.
    assert.deepEqual(second.installed, ["skill-a"]);
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

  test("de-activation: a skill withheld by the server (active→rejected/quarantined) is removed from disk next tick", async () => {
    // Tick 1: skill is active → server returns it → lands on disk.
    plantOnDisk("memclaw");
    mockCatalog = [
      { doc_id: "deploy-runbook", data: { name: "deploy-runbook", description: "deploy steps", content: "# deploy\n" } },
    ];
    const first = await reconcileSkills();
    assert.deepEqual(first.added, ["deploy-runbook"]);
    assert.deepEqual(first.installed, ["deploy-runbook"]);
    assert.ok(listSkillDirs().includes("deploy-runbook"));

    // Tick 2: the skill flipped to a non-active status, so the install
    // surface stops returning it. The reconciler must remove it from
    // disk — keeping push (disk) in sync with the active-only gate.
    mockCatalog = [];
    const second = await reconcileSkills();
    assert.deepEqual(second.removed, ["deploy-runbook"]);
    // The heartbeat now reports an empty installed set — the operator
    // sees the skill is no longer live on this node.
    assert.deepEqual(second.installed, []);
    assert.deepEqual(listSkillDirs(), ["memclaw"]); // gone; bundled survives
  });

  test("server fail-closed (503) → reconciler fails safe: disk preserved, nothing pushed", async () => {
    // The install surface raises 503 during a settings outage (fail
    // closed). apiCall throws on non-2xx, so the reconciler catches it
    // and leaves disk untouched — no non-active skill can be pushed.
    plantOnDisk("memclaw");
    plantOnDisk("skill-a", "# from a healthy prior tick\n");
    globalThis.fetch = (async () =>
      new Response("skill lifecycle gate unavailable", { status: 503 })) as typeof fetch;

    const summary = await reconcileSkills();

    assert.deepEqual(listSkillDirs(), ["memclaw", "skill-a"]); // untouched
    assert.equal(summary.catalogCount, 0);
    assert.deepEqual(summary.added, []);
    assert.deepEqual(summary.removed, []);
  });

  test("installed reflects CONFIRMED disk, not desired intent: a failed write is excluded", async () => {
    plantOnDisk("memclaw");
    // Plant a FILE where the reconciler wants a directory, so
    // mkdirSync(skills/bad-skill) throws and its write fails — without
    // mocking fs. ``good-skill`` writes cleanly.
    writeFileSync(join(SKILLS_ROOT, "bad-skill"), "i am a file, not a dir\n", "utf-8");
    mockCatalog = [
      { doc_id: "good-skill", data: { name: "good-skill", description: "ok",  content: "# good\n" } },
      { doc_id: "bad-skill",  data: { name: "bad-skill",  description: "nope", content: "# bad\n" } },
    ];

    const summary = await reconcileSkills();

    // The failed write must NOT be reported as installed (it never
    // converged to disk); the clean one must be.
    assert.deepEqual(summary.installed, ["good-skill"]);
    assert.ok(summary.added.includes("good-skill"));
    assert.ok(!summary.added.includes("bad-skill"));
  });
});

describe("resolveSkillTargets (config plumbing)", () => {
  afterEach(() => {
    delete process.env.MEMCLAW_SKILL_TARGETS;
  });

  test("default (unset): single owned target = the plugin skills dir", () => {
    delete process.env.MEMCLAW_SKILL_TARGETS;
    const targets = resolveSkillTargets();
    assert.equal(targets.length, 1);
    assert.equal(targets[0].dir, SKILLS_ROOT);
    assert.equal(targets[0].mode, "owned");
  });

  test("valid JSON appends extra targets (owned + additive)", () => {
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([
      { dir: "/tmp/wt-a", mode: "additive" },
      { dir: "/tmp/wt-b", mode: "owned" },
    ]);
    const targets = resolveSkillTargets();
    assert.equal(targets[0].dir, SKILLS_ROOT); // owned dir always first
    assert.deepEqual(
      targets.slice(1).map((t) => [t.dir, t.mode]),
      [["/tmp/wt-a", "additive"], ["/tmp/wt-b", "owned"]],
    );
  });

  test("invalid JSON → fail safe to owned-only", () => {
    process.env.MEMCLAW_SKILL_TARGETS = "{not valid json";
    const targets = resolveSkillTargets();
    assert.equal(targets.length, 1);
    assert.equal(targets[0].mode, "owned");
  });

  test("non-array JSON → owned-only", () => {
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify({ dir: "/tmp/x", mode: "owned" });
    assert.equal(resolveSkillTargets().length, 1);
  });

  test("malformed entries skipped (missing dir / invalid mode)", () => {
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([
      { mode: "owned" }, // missing dir
      { dir: "/tmp/x", mode: "weird" }, // invalid mode
      { dir: "/tmp/ok", mode: "additive" }, // valid
    ]);
    const targets = resolveSkillTargets();
    assert.equal(targets.length, 2); // owned default + /tmp/ok
    assert.deepEqual([targets[1].dir, targets[1].mode], ["/tmp/ok", "additive"]);
  });

  test("an entry duplicating the owned dir is dropped", () => {
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([{ dir: SKILLS_ROOT, mode: "owned" }]);
    assert.equal(resolveSkillTargets().length, 1);
  });

  test("too-shallow target dirs (/, /tmp) are skipped — owned-mode rmSync guard", () => {
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([
      { dir: "/", mode: "owned" }, // 0 segments
      { dir: "/tmp", mode: "owned" }, // 1 segment
      { dir: "/tmp/skills-ok", mode: "additive" }, // 2 segments → kept
    ]);
    const targets = resolveSkillTargets();
    assert.equal(targets.length, 2); // owned default + /tmp/skills-ok
    assert.deepEqual([targets[1].dir, targets[1].mode], ["/tmp/skills-ok", "additive"]);
  });
});

describe("reconcileSkills — configured targets", () => {
  const EXTERNAL = join(tmpHome, "external-skills");
  const EXTRA_OWNED = join(tmpHome, "extra-owned-skills");
  const wipe = (d: string) => { if (existsSync(d)) rmSync(d, { recursive: true, force: true }); };

  beforeEach(() => {
    resetSkillsDir();
    wipe(EXTERNAL);
    wipe(EXTRA_OWNED);
    installMockFetch();
    mockCatalog = [];
  });

  afterEach(() => {
    restoreFetch();
    delete process.env.MEMCLAW_SKILL_TARGETS;
  });

  test("additive target is parsed but SKIPPED (not implemented) — foreign dir untouched", async () => {
    plantOnDisk("memclaw");
    // a client-owned skill living in the additive target dir
    mkdirSync(join(EXTERNAL, "client-skill"), { recursive: true });
    writeFileSync(join(EXTERNAL, "client-skill", "SKILL.md"), "# client owned\n", "utf-8");
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([{ dir: EXTERNAL, mode: "additive" }]);
    mockCatalog = [
      { doc_id: "deploy-runbook", data: { name: "deploy-runbook", description: "d", content: "# deploy\n" } },
    ];

    const summary = await reconcileSkills();

    // Owned dir reconciles normally.
    assert.ok(listSkillDirs().includes("deploy-runbook"));
    assert.deepEqual(summary.installed, ["deploy-runbook"]);
    // The additive dir + its foreign skill are completely untouched: not
    // pruned, not overwritten, and the catalog skill is NOT written there.
    assert.equal(
      readFileSync(join(EXTERNAL, "client-skill", "SKILL.md"), "utf-8"),
      "# client owned\n",
    );
    assert.ok(!existsSync(join(EXTERNAL, "deploy-runbook")));
  });

  test("an extra owned target is reconciled alongside the default", async () => {
    plantOnDisk("memclaw");
    process.env.MEMCLAW_SKILL_TARGETS = JSON.stringify([{ dir: EXTRA_OWNED, mode: "owned" }]);
    mockCatalog = [
      { doc_id: "deploy-runbook", data: { name: "deploy-runbook", description: "d", content: "# deploy\n" } },
    ];

    await reconcileSkills();

    // Written into BOTH the default owned dir and the extra owned dir.
    assert.ok(existsSync(join(SKILLS_ROOT, "deploy-runbook", "SKILL.md")));
    assert.ok(existsSync(join(EXTRA_OWNED, "deploy-runbook", "SKILL.md")));
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
