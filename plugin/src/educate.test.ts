import { test, describe, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync, statSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { tmpdir } from "os";
import { educateAgents, writeEducationFiles } from "./index.js";
import {
  buildToolsMd,
  buildAgentsMd,
  discoverAgentWorkspaces,
  cleanupStaleHeartbeatEducation,
} from "./educate.js";
import { MEMCLAW_TOOLS } from "./tools.js";
import { MEMORY_TYPES, STATUSES } from "./tool-definitions.js";

// Resolve the shared SKILL.md that ships with the plugin. Tests run from
// `plugin/dist/educate.test.js`; the skill lives at
// `plugin/skills/memclaw/SKILL.md`.
const __dirname = dirname(fileURLToPath(import.meta.url));
const SHARED_SKILL_PATH = join(__dirname, "..", "skills", "memclaw", "SKILL.md");
function readSharedSkill(): string {
  return readFileSync(SHARED_SKILL_PATH, "utf-8");
}

function makeTmpBase(): string {
  return mkdtempSync(join(tmpdir(), "educate-test-"));
}

describe("educateAgents", () => {
  const dirs: string[] = [];
  function tmpBase(): string {
    const d = makeTmpBase();
    dirs.push(d);
    return d;
  }
  afterEach(() => {
    for (const d of dirs) {
      try { rmSync(d, { recursive: true, force: true }); } catch {}
    }
    dirs.length = 0;
  });

  test("writes to default workspace", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });

    const result = educateAgents("hello agents", undefined, base);

    assert.equal(result.verified, 1);
    assert.equal(result.count, 1);
    assert.deepEqual(result.educated, ["workspace"]);
    assert.equal(result.failed.length, 0);

    const content = readFileSync(join(base, "workspace", "HEARTBEAT.md"), "utf-8");
    assert.ok(content.includes("hello agents"));
  });

  test("writes to per-agent workspaces", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspaces", "agent-1"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent-2"), { recursive: true });

    const result = educateAgents("learn this", undefined, base);

    assert.equal(result.verified, 2);
    assert.equal(result.count, 2);
    assert.ok(result.educated.includes("agent-1"));
    assert.ok(result.educated.includes("agent-2"));

    for (const agent of ["agent-1", "agent-2"]) {
      const content = readFileSync(join(base, "workspaces", agent, "HEARTBEAT.md"), "utf-8");
      assert.ok(content.includes("learn this"));
    }
  });

  test("writes to both default and per-agent workspaces", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent-1"), { recursive: true });

    const result = educateAgents("broadcast", undefined, base);

    assert.equal(result.verified, 2);
    assert.ok(result.educated.includes("workspace"));
    assert.ok(result.educated.includes("agent-1"));
  });

  test("filters by agentIds", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspaces", "agent-1"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent-2"), { recursive: true });

    const result = educateAgents("selective", ["agent-2"], base);

    assert.equal(result.verified, 1);
    assert.deepEqual(result.educated, ["agent-2"]);
  });

  test("appends with separator to existing content", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });
    writeFileSync(join(base, "workspace", "HEARTBEAT.md"), "existing stuff\n", "utf-8");

    educateAgents("new prompt", undefined, base);

    const content = readFileSync(join(base, "workspace", "HEARTBEAT.md"), "utf-8");
    assert.ok(content.includes("existing stuff"));
    assert.ok(content.includes("---"));
    assert.ok(content.includes("new prompt"));
  });

  test("returns zero when no workspaces exist", () => {
    const base = tmpBase();
    // empty dir, no workspace or workspaces/

    const result = educateAgents("nobody home", undefined, base);

    assert.equal(result.verified, 0);
    assert.equal(result.count, 0);
    assert.equal(result.educated.length, 0);
  });
});

describe("discoverAgentWorkspaces", () => {
  const dirs: string[] = [];
  function tmpBase(): string {
    const d = makeTmpBase();
    dirs.push(d);
    return d;
  }
  afterEach(() => {
    for (const d of dirs) {
      try { rmSync(d, { recursive: true, force: true }); } catch {}
    }
    dirs.length = 0;
  });

  test("finds the default <baseDir>/workspace as id=main", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });

    const ws = discoverAgentWorkspaces(base);

    assert.equal(ws.length, 1);
    assert.equal(ws[0].id, "main");
    assert.equal(ws[0].path, join(base, "workspace"));
  });

  test("finds <baseDir>/workspace-<name> as id=<name> (hyphen-prefix)", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace-alpha"), { recursive: true });
    mkdirSync(join(base, "workspace-beta"), { recursive: true });

    const ws = discoverAgentWorkspaces(base);
    const ids = ws.map((w) => w.id).sort();

    assert.deepEqual(ids, ["alpha", "beta"]);
  });

  test("finds <baseDir>/workspaces/<name>/ subdirs (the openclaw agents add path)", () => {
    // Regression: the previous writeEducationFiles discovery walked baseDir
    // entries with `startsWith("workspace")` and did NOT recurse into
    // <baseDir>/workspaces/, so subdir agents were silently skipped.
    const base = tmpBase();
    mkdirSync(join(base, "workspaces", "agent-a"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent-b"), { recursive: true });

    const ws = discoverAgentWorkspaces(base);
    const ids = ws.map((w) => w.id).sort();

    assert.deepEqual(ids, ["agent-a", "agent-b"]);
    for (const w of ws) {
      assert.ok(w.path.includes("/workspaces/"), `subdir agent path should be under workspaces/: ${w.path}`);
    }
  });

  test("does NOT return the literal <baseDir>/workspaces parent dir as a workspace", () => {
    // Regression: the pre-fix code matched `"workspaces".startsWith("workspace")`
    // and treated the plural parent dir itself as a workspace, leading to
    // phantom education files at <baseDir>/workspaces/{TOOLS,AGENTS}.md.
    const base = tmpBase();
    mkdirSync(join(base, "workspaces"), { recursive: true });

    const ws = discoverAgentWorkspaces(base);

    for (const w of ws) {
      assert.notEqual(w.path, join(base, "workspaces"));
    }
  });

  test("honors agents.list[].workspace from openclaw.json", () => {
    const base = tmpBase();
    const customWs = join(base, "custom-ws-dir");
    mkdirSync(customWs, { recursive: true });
    writeFileSync(
      join(base, "openclaw.json"),
      JSON.stringify({
        agents: { list: [{ id: "custom-agent", workspace: customWs }] },
      }),
      "utf-8",
    );

    const ws = discoverAgentWorkspaces(base);

    assert.equal(ws.length, 1);
    assert.equal(ws[0].id, "custom-agent");
    assert.equal(ws[0].path, customWs);
  });

  test("dedups overlapping discoveries — agents.list + workspaces/<name>", () => {
    const base = tmpBase();
    const wsPath = join(base, "workspaces", "agent2");
    mkdirSync(wsPath, { recursive: true });
    writeFileSync(
      join(base, "openclaw.json"),
      JSON.stringify({
        agents: { list: [{ id: "agent2", workspace: wsPath }] },
      }),
      "utf-8",
    );

    const ws = discoverAgentWorkspaces(base);

    assert.equal(ws.length, 1, "expected dedup to a single entry");
    assert.equal(ws[0].id, "agent2");
  });

  test("filters by agentIds when set is non-empty", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent2"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent3"), { recursive: true });

    const onlyAgent2 = discoverAgentWorkspaces(base, ["agent2"]);
    const ids = onlyAgent2.map((w) => w.id).sort();

    assert.deepEqual(ids, ["agent2"]);
  });

  test("rejects paths outside baseDir", () => {
    const base = tmpBase();
    const outside = mkdtempSync(join(tmpdir(), "outside-"));
    dirs.push(outside);
    writeFileSync(
      join(base, "openclaw.json"),
      JSON.stringify({
        agents: { list: [{ id: "evil", workspace: outside }] },
      }),
      "utf-8",
    );

    const ws = discoverAgentWorkspaces(base);

    assert.equal(ws.length, 0, "path outside baseDir must be rejected");
  });
});

describe("writeEducationFiles", () => {
  // These are regression tests for the multi-agent education bug
  // (memclaw memory 90b7d579-068a-4561-a740-73a76a74b1ac). The old
  // implementation discovered workspaces via `readdirSync(baseDir)` filtered
  // by `startsWith("workspace")`, which silently skipped agents living under
  // `<baseDir>/workspaces/<name>/` and also wrote phantom education files
  // into `<baseDir>/workspaces/{TOOLS,AGENTS}.md` because the literal
  // `workspaces` directory matched the filter.

  const dirs: string[] = [];
  function tmpBase(): string {
    const d = makeTmpBase();
    dirs.push(d);
    return d;
  }
  afterEach(() => {
    for (const d of dirs) {
      try { rmSync(d, { recursive: true, force: true }); } catch {}
    }
    dirs.length = 0;
  });

  function setupAllFourPatterns(base: string): {
    main: string;
    hyphen: string;
    customFromConfig: string;
    subdir: string;
  } {
    const main = join(base, "workspace");
    const hyphen = join(base, "workspace-hyphenated");
    const customFromConfig = join(base, "agents", "custom-place");
    const subdir = join(base, "workspaces", "agent2");
    for (const d of [main, hyphen, customFromConfig, subdir]) {
      mkdirSync(d, { recursive: true });
    }
    writeFileSync(
      join(base, "openclaw.json"),
      JSON.stringify({
        agents: {
          list: [
            { id: "main" },
            { id: "configured", workspace: customFromConfig },
            { id: "agent2", workspace: subdir },
          ],
        },
      }),
      "utf-8",
    );
    return { main, hyphen, customFromConfig, subdir };
  }

  test("educates ALL four workspace patterns (the bug fix)", () => {
    const base = tmpBase();
    const ws = setupAllFourPatterns(base);

    const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.equal(result.toolsUpdated, 4, `expected 4 TOOLS.md writes, got ${result.toolsUpdated}`);
    assert.equal(result.agentsUpdated, 4, `expected 4 AGENTS.md writes, got ${result.agentsUpdated}`);

    for (const wsPath of [ws.main, ws.hyphen, ws.customFromConfig, ws.subdir]) {
      const tools = readFileSync(join(wsPath, "TOOLS.md"), "utf-8");
      const agents = readFileSync(join(wsPath, "AGENTS.md"), "utf-8");
      assert.ok(tools.includes("MemClaw"), `TOOLS.md missing MemClaw section in ${wsPath}`);
      assert.ok(agents.includes("## Memory V2"), `AGENTS.md missing '## Memory V2' anchor in ${wsPath}`);
    }
  });

  test("subdir-of-plural agents are no longer silently skipped (regression)", () => {
    const base = tmpBase();
    const subdir = join(base, "workspaces", "agent-x");
    mkdirSync(subdir, { recursive: true });

    const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.equal(result.toolsUpdated, 1);
    assert.equal(result.agentsUpdated, 1);
    assert.ok(readFileSync(join(subdir, "TOOLS.md"), "utf-8").includes("MemClaw"));
    assert.ok(readFileSync(join(subdir, "AGENTS.md"), "utf-8").includes("## Memory V2"));
  });

  test("does NOT create phantom files at <baseDir>/workspaces/{TOOLS,AGENTS}.md", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspaces", "agent-x"), { recursive: true });

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.ok(
      !existsSync(join(base, "workspaces", "TOOLS.md")),
      "phantom TOOLS.md must NOT be created at <baseDir>/workspaces/",
    );
    assert.ok(
      !existsSync(join(base, "workspaces", "AGENTS.md")),
      "phantom AGENTS.md must NOT be created at <baseDir>/workspaces/",
    );
  });

  test("idempotent: re-running does not double-append", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);
    const firstTools = readFileSync(join(base, "workspace", "TOOLS.md"), "utf-8");
    const firstAgents = readFileSync(join(base, "workspace", "AGENTS.md"), "utf-8");

    const second = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.equal(second.toolsUpdated, 0);
    assert.equal(second.agentsUpdated, 0);
    assert.equal(readFileSync(join(base, "workspace", "TOOLS.md"), "utf-8"), firstTools);
    assert.equal(readFileSync(join(base, "workspace", "AGENTS.md"), "utf-8"), firstAgents);
  });

  test("respects agentIds filter (educates only requested workspaces)", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspace"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent2"), { recursive: true });
    mkdirSync(join(base, "workspaces", "agent3"), { recursive: true });

    const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), ["agent2"], base);

    assert.equal(result.toolsUpdated, 1);
    assert.equal(result.agentsUpdated, 1);
    assert.ok(readFileSync(join(base, "workspaces", "agent2", "TOOLS.md"), "utf-8").includes("MemClaw"));
    assert.ok(!existsSync(join(base, "workspace", "TOOLS.md")) || !readFileSync(join(base, "workspace", "TOOLS.md"), "utf-8").includes("MemClaw"));
    assert.ok(!existsSync(join(base, "workspaces", "agent3", "TOOLS.md")) || !readFileSync(join(base, "workspaces", "agent3", "TOOLS.md"), "utf-8").includes("MemClaw"));
  });

  test("cleans up pre-existing phantom MemClaw files at <baseDir>/workspaces/", () => {
    const base = tmpBase();
    mkdirSync(join(base, "workspaces"), { recursive: true });
    writeFileSync(join(base, "workspaces", "TOOLS.md"), buildToolsMd(), "utf-8");
    writeFileSync(join(base, "workspaces", "AGENTS.md"), buildAgentsMd(), "utf-8");

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.ok(!existsSync(join(base, "workspaces", "TOOLS.md")));
    assert.ok(!existsSync(join(base, "workspaces", "AGENTS.md")));
  });

  test("cleanup leaves non-MemClaw content at <baseDir>/workspaces/ alone", () => {
    // Defensive: if a user happens to have unrelated TOOLS.md/AGENTS.md at
    // that path, we must not delete it. Cleanup is gated on memclaw markers.
    const base = tmpBase();
    mkdirSync(join(base, "workspaces"), { recursive: true });
    writeFileSync(join(base, "workspaces", "TOOLS.md"), "# my own tools notes\n", "utf-8");
    writeFileSync(join(base, "workspaces", "AGENTS.md"), "# my own agents notes\n", "utf-8");

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.equal(
      readFileSync(join(base, "workspaces", "TOOLS.md"), "utf-8"),
      "# my own tools notes\n",
    );
    assert.equal(
      readFileSync(join(base, "workspaces", "AGENTS.md"), "utf-8"),
      "# my own agents notes\n",
    );
  });

  // ── Fenced-block migration (A1) ──
  //
  // Pre-A1 idempotency was substring-based (`includes("MemClaw")` /
  // `includes("## Memory V2")`), so once a workspace had any MemClaw
  // content, plugin upgrades never refreshed it. Below: every behaviour
  // the new versioned-fence implementation must guarantee.

  describe("fenced-block migration", () => {
    function readFile(p: string): string {
      return readFileSync(p, "utf-8");
    }

    test("fresh install writes a fenced block with the current version tag", () => {
      const base = tmpBase();
      mkdirSync(join(base, "workspace"), { recursive: true });

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      const tools = readFile(join(base, "workspace", "TOOLS.md"));
      const agents = readFile(join(base, "workspace", "AGENTS.md"));
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/);
      assert.match(tools, /<!-- \/memclaw:tools -->/);
      assert.match(agents, /<!-- memclaw:agents v=[a-f0-9]{8} -->/);
      assert.match(agents, /<!-- \/memclaw:agents -->/);
    });

    test("re-run on current-version fence is a no-op (toolsUpdated=0, file byte-identical)", () => {
      const base = tmpBase();
      mkdirSync(join(base, "workspace"), { recursive: true });

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);
      const beforeTools = readFile(join(base, "workspace", "TOOLS.md"));
      const beforeAgents = readFile(join(base, "workspace", "AGENTS.md"));

      const second = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(second.toolsUpdated, 0);
      assert.equal(second.agentsUpdated, 0);
      assert.equal(readFile(join(base, "workspace", "TOOLS.md")), beforeTools);
      assert.equal(readFile(join(base, "workspace", "AGENTS.md")), beforeAgents);
    });

    test("stale-version fence is replaced in place; surrounding user content preserved byte-for-byte", () => {
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore =
        "# Workspace tools\n\n" +
        "Some user notes about tooling that predate MemClaw.\n\n";
      const userAfter =
        "\n\n## Notes\n\nMore user content below the MemClaw section.\n";
      const staleFenced =
        "<!-- memclaw:tools v=deadbeef -->\n" +
        "## MemClaw — Tools Available\n\nObsolete content from a previous version.\n" +
        "<!-- /memclaw:tools -->\n";
      writeFileSync(
        join(wsDir, "TOOLS.md"),
        userBefore + staleFenced + userAfter,
        "utf-8",
      );

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1, "stale fence must trigger a write");
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(tools.startsWith(userBefore), "user content above the fence must be preserved verbatim");
      assert.ok(tools.endsWith(userAfter), "user content below the fence must be preserved verbatim");
      assert.ok(!tools.includes("v=deadbeef"), "stale version tag must be replaced");
      assert.ok(!tools.includes("Obsolete content"), "stale block content must be replaced");
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/);
    });

    test("stale-version fence replace also writes a one-shot .memclaw-bak", () => {
      // Pre-fix the case-2 (stale-version) replace had no backup, so a
      // user who hand-edited inside the fenced block would silently
      // lose those edits on plugin upgrade. Mirror the case-3 behaviour.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore = "# Tools\n\nMy notes.\n\n";
      const handEditedFence =
        "<!-- memclaw:tools v=deadbeef -->\n" +
        "## MemClaw — Tools Available\n\n" +
        "OLD CONTENT plus IMPORTANT user note that I added myself.\n" +
        "<!-- /memclaw:tools -->\n";
      const original = userBefore + handEditedFence;
      writeFileSync(join(wsDir, "TOOLS.md"), original, "utf-8");

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      const bakPath = join(wsDir, "TOOLS.md.memclaw-bak");
      assert.ok(
        existsSync(bakPath),
        "stale-version replace must write a backup so hand-edits inside the fence are recoverable",
      );
      assert.equal(readFile(bakPath), original, "backup must be the pre-replace content");
      // And the user's hand-edit is recoverable from the backup even
      // though it's no longer in the live file.
      assert.ok(
        readFile(bakPath).includes("IMPORTANT user note"),
        "hand-edited content recoverable from backup",
      );
    });

    test("legacy v0.98.5 heading variant is detected via prefix match", () => {
      // Wet-test on openclaw-test-ran (CAURA-333) revealed the plugin
      // had emitted an older heading form on 0.98.5:
      //   "## MemClaw — Long-Term Agent Memory (auto-added by plugin)"
      // for TOOLS.md, and a different form for AGENTS.md. Pre-fix the
      // legacy splice was exact-match on the 1.x heading text, so 20
      // workspaces ended up with legacy + new fence side-by-side.
      // Prefix matching catches every historical form.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore = "# TOOLS.md - Local Notes\n\nMy notes here.\n";
      const legacy_v0_98 =
        "\n---\n\n" +
        "## MemClaw — Long-Term Agent Memory (auto-added by plugin)\n\n" +
        "13 tools:\n" +
        "| `memclaw_write_bulk` | Store multiple memories at once |\n" +
        "| `memclaw_search` | Semantic search, returns raw results |\n";
      writeFileSync(join(wsDir, "TOOLS.md"), userBefore + legacy_v0_98, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1, "legacy v0.98.5 heading must be detected and spliced");
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(!tools.includes("memclaw_write_bulk"), "stale 0.98.5 tool name must be replaced");
      assert.ok(!tools.includes("Long-Term Agent Memory"), "stale 0.98.5 heading must be gone");
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/);
      assert.ok(tools.startsWith(userBefore), "user content above legacy block preserved");
      assert.ok(existsSync(join(wsDir, "TOOLS.md.memclaw-bak")), "v0.98.5 splice must write backup");
    });

    test("legacy v0.98.5 AGENTS.md heading variant is detected via prefix match", () => {
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore = "# AGENTS.md\n\n## Identity\n\nI am Claude.\n";
      const legacy_v0_98 =
        "\n---\n\n" +
        "## Memory V2 (auto-added by MemClaw plugin — replaces any earlier memory section above)\n\n" +
        "**Layer 1:** Per-turn write-back — `memclaw_write` after every meaningful outcome\n" +
        "- Update > Create — prefer `memclaw_update` over duplicates\n";
      writeFileSync(join(wsDir, "AGENTS.md"), userBefore + legacy_v0_98, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.agentsUpdated, 1);
      const agents = readFile(join(wsDir, "AGENTS.md"));
      assert.ok(!agents.includes("auto-added by MemClaw plugin"), "stale 0.98.5 heading must be gone");
      assert.ok(!agents.includes("memclaw_update"), "stale 0.98.5 body must be replaced");
      assert.match(agents, /<!-- memclaw:agents v=[a-f0-9]{8} -->/);
      assert.ok(agents.startsWith(userBefore), "user content above legacy block preserved");
    });

    test("user heading starting with the same prefix but no MemClaw content is left alone", () => {
      // Defensive: a user happens to write `## MemClaw — my own notes`
      // with no `memclaw_*` references in the body. The content-shape
      // check rejects the splice. We append a fresh fenced block at
      // EOF instead of replacing the user's heading.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userContent =
        "# Tools\n\n" +
        "## MemClaw — my own notes\n\n" +
        "Just my own notes about Claude/MemClaw deployment, no tools listed.\n";
      writeFileSync(join(wsDir, "TOOLS.md"), userContent, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1, "should append fenced block since legacy splice was skipped");
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(tools.includes("## MemClaw — my own notes"), "user heading must be preserved");
      assert.ok(tools.includes("Just my own notes"), "user content under that heading must be preserved");
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/, "fresh fence appended");
    });

    test("CRLF line endings: legacy heading is still detected and spliced", () => {
      // Files authored on Windows use \r\n; under split('\\n') each
      // `lines[i]` carries a trailing \r and an exact-equality heading
      // match would fail. Targeted CR-trim in findLegacyRange's match
      // step is the fix; offset math is unchanged because line.length
      // already includes the trailing \r.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore = "# Tools\r\n\r\n";
      const legacy =
        "---\r\n\r\n## MemClaw — Tools Available\r\n\r\n" +
        "Obsolete CRLF body with memclaw_write_bulk reference.\r\n";
      writeFileSync(join(wsDir, "TOOLS.md"), userBefore + legacy, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1, "CRLF legacy heading must still be detected");
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(!tools.includes("memclaw_write_bulk"), "stale CRLF content must be replaced");
      assert.ok(tools.startsWith(userBefore), "user content above legacy block preserved (with original CRLF)");
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/);
    });

    test("legacy pre-fence section is migrated; one-shot .memclaw-bak is written", () => {
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      // A v1.x-shaped TOOLS.md: user content, then the unfenced legacy
      // MemClaw section (preceded by `---` rule), then more user content.
      const userBefore = "# Tools notes\n\nMy tools notes.\n";
      const legacy =
        "\n---\n\n## MemClaw — Tools Available\n\n" +
        "Obsolete 13-tool listing including memclaw_write_bulk and memclaw_search.\n";
      const userAfter = "\n## Other\n\nMy other section.\n";
      const original = userBefore + legacy + userAfter;
      writeFileSync(join(wsDir, "TOOLS.md"), original, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1);
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(!tools.includes("memclaw_write_bulk"), "legacy stale tool name must be gone");
      assert.ok(!tools.includes("memclaw_search"), "legacy stale tool name must be gone");
      assert.match(tools, /<!-- memclaw:tools v=[a-f0-9]{8} -->/);
      assert.ok(tools.startsWith(userBefore), "content above the legacy block must be preserved");
      assert.ok(tools.includes("## Other"), "content below the legacy block must be preserved");

      // Backup written exactly once with the pre-splice content.
      const bakPath = join(wsDir, "TOOLS.md.memclaw-bak");
      assert.ok(existsSync(bakPath), "expected one-shot .memclaw-bak after legacy splice");
      assert.equal(readFile(bakPath), original);
    });

    test("backup is not overwritten on subsequent runs", () => {
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const original =
        // Body must include a `memclaw_` token so the content-shape
        // check in findLegacyRange treats this as our block (and not
        // a coincidental user heading with the same prefix).
        "user\n\n---\n\n## MemClaw — Tools Available\n\nold body referencing memclaw_write_bulk.\n";
      writeFileSync(join(wsDir, "TOOLS.md"), original, "utf-8");

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);
      const bakPath = join(wsDir, "TOOLS.md.memclaw-bak");
      const bak1 = readFile(bakPath);

      // Hand-edit the live TOOLS.md and re-run — backup must NOT change.
      writeFileSync(join(wsDir, "TOOLS.md"), readFile(join(wsDir, "TOOLS.md")) + "\nappended\n", "utf-8");
      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(readFile(bakPath), bak1, "backup must not be overwritten on subsequent runs");
    });

    test("force=true rewrites even when the version matches", () => {
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);
      const second = writeEducationFiles(
        buildToolsMd(),
        buildAgentsMd(),
        undefined,
        base,
        { force: true },
      );
      assert.equal(second.toolsUpdated, 1, "force=true must rewrite even on matching version");
      assert.equal(second.agentsUpdated, 1, "force=true must rewrite even on matching version");
    });

    test("force=true with matching version still writes backup (preserves hand-edits)", () => {
      // Design intent: a user can hand-edit content INSIDE a fenced
      // block without changing the version tag. force=true is the
      // operator's way to revert that edit to canonical content. The
      // backup MUST be written so the user can recover their edit if
      // the revert was accidental — even though `fenceMatch[1]`
      // equals the freshly-computed `version`. (Without this guard,
      // the obvious "skip backup when version matches" optimisation
      // would silently lose user data.)
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);
      // Read the just-written canonical TOOLS.md and inject a
      // user-authored note INSIDE the fence (between the open and
      // close markers). The version tag remains unchanged.
      const toolsPath = join(wsDir, "TOOLS.md");
      const canonical = readFile(toolsPath);
      const closeIdx = canonical.indexOf("<!-- /memclaw:tools -->");
      assert.ok(closeIdx > 0, "expected close marker in canonical output");
      const handEdit = "\nIMPORTANT user note inside the fence — do not lose me.\n";
      const handEdited =
        canonical.slice(0, closeIdx) + handEdit + canonical.slice(closeIdx);
      writeFileSync(toolsPath, handEdited, "utf-8");

      const result = writeEducationFiles(
        buildToolsMd(),
        buildAgentsMd(),
        undefined,
        base,
        { force: true },
      );

      // Backup MUST exist and MUST contain the user's hand-edit.
      const bakPath = toolsPath + ".memclaw-bak";
      assert.ok(
        existsSync(bakPath),
        "force=true with matching version MUST still write a backup so hand-edits inside the fence are recoverable",
      );
      assert.ok(
        readFile(bakPath).includes("IMPORTANT user note inside the fence"),
        "backup must contain the pre-rewrite content (with user's hand-edit)",
      );
      // Live file is reverted to canonical (no hand-edit).
      assert.ok(
        !readFile(toolsPath).includes("IMPORTANT user note inside the fence"),
        "force=true reverted the live file to canonical content",
      );
      assert.equal(result.toolsUpdated, 1);
    });

    test("unmatched fence opener (user-authored) is treated as no-fence and appended", () => {
      // If a user happens to have something that looks like our opener
      // (e.g. they hand-added `<!-- memclaw:tools -->` for a comment) but
      // never closed it, we must not eat their content. Treat it as no
      // fence and append at EOF — same as a fresh install.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });
      const userContent = "<!-- memclaw:tools v=garbage -->\nuser note, unclosed\n";
      writeFileSync(join(wsDir, "TOOLS.md"), userContent, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.toolsUpdated, 1);
      const tools = readFile(join(wsDir, "TOOLS.md"));
      assert.ok(tools.startsWith(userContent), "user's unclosed-opener content must be preserved at the top");
      assert.match(tools, /<!-- \/memclaw:tools -->\n?$/);
    });

    test("AGENTS.md follows the same fence semantics", () => {
      // Same matrix, AGENTS.md side: stale-fence replace + legacy-bridge
      // + backup. One consolidated test rather than mirroring every case.
      const base = tmpBase();
      const wsDir = join(base, "workspace");
      mkdirSync(wsDir, { recursive: true });

      const userBefore = "# Agents.md\n\n## Routing\n\nDo this.\n";
      // Body must include a `memclaw_` token so the content-shape
      // check in findLegacyRange treats this as our block.
      const legacy =
        "\n---\n\n## Memory V2 — MemClaw Protocol (mandatory)\n\nObsolete agent rules using memclaw_write.\n";
      writeFileSync(join(wsDir, "AGENTS.md"), userBefore + legacy, "utf-8");

      const result = writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

      assert.equal(result.agentsUpdated, 1);
      const agents = readFile(join(wsDir, "AGENTS.md"));
      assert.ok(!agents.includes("Obsolete agent rules"), "legacy AGENTS.md content must be replaced");
      assert.match(agents, /<!-- memclaw:agents v=[a-f0-9]{8} -->/);
      assert.ok(agents.startsWith(userBefore), "user content above the legacy block must be preserved");
      assert.ok(existsSync(join(wsDir, "AGENTS.md.memclaw-bak")));
    });
  });
});

// ── HEARTBEAT.md cleanup (C1) ──
//
// Pre-C1 the plugin's first-load auto-education wrote a 4-sentence
// MemClaw paragraph into each workspace's HEARTBEAT.md, fully
// redundant with TOOLS.md/AGENTS.md/SKILL.md and unfenced. C1 stops
// that write and one-shot cleans the legacy paragraph from existing
// installs whenever writeEducationFiles runs.

describe("cleanupStaleHeartbeatEducation (unit)", () => {
  test("removes a paragraph appended with leading separator", () => {
    const userContent = "# Heartbeat\n\n## Cron tasks\n- Task 1\n- Task 2";
    const stale =
      "\n\n---\n\nYou have been connected to MemClaw — a shared persistent memory system. " +
      "You now have 9 tools for writing, searching, and managing memories. " +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.\n";

    const result = cleanupStaleHeartbeatEducation(userContent + stale);

    assert.equal(result.cleaned, true);
    assert.equal(
      result.content,
      userContent,
      "user content above the paragraph must be preserved verbatim",
    );
  });

  test("removes a paragraph that is the entire file (first-install case)", () => {
    // educateAgents writes only `<prompt>\n` when HEARTBEAT.md was empty.
    const onlyParagraph =
      "You have been connected to MemClaw — a shared persistent memory system. " +
      "You now have 10 tools for writing, searching, and managing memories. " +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.\n";

    const result = cleanupStaleHeartbeatEducation(onlyParagraph);

    assert.equal(result.cleaned, true);
    assert.equal(result.content, "", "file with only the paragraph cleans to empty");
  });

  test("matches paragraphs with any tool count (regex covers N drift)", () => {
    // The stale paragraph hard-codes the tool count at write time.
    // Cleanup must work whether the count was 9, 10, 13, etc.
    for (const n of [7, 9, 10, 13, 99]) {
      const para =
        "You have been connected to MemClaw — a shared persistent memory system. " +
        `You now have ${n} tools for writing, searching, and managing memories. ` +
        "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
        "starting work, always write findings after completing work, always include your agent_id.\n";
      const result = cleanupStaleHeartbeatEducation(para);
      assert.equal(result.cleaned, true, `failed to clean paragraph with N=${n}`);
      assert.equal(result.content, "");
    }
  });

  test("no-op on HEARTBEAT.md without the paragraph", () => {
    const content = "# Heartbeat\n\n## Cron tasks\n- Task 1\n";
    const result = cleanupStaleHeartbeatEducation(content);
    assert.equal(result.cleaned, false);
    assert.equal(result.content, content);
  });

  test("no-op on empty content", () => {
    const result = cleanupStaleHeartbeatEducation("");
    assert.equal(result.cleaned, false);
    assert.equal(result.content, "");
  });

  test("does NOT match a coincidental mention of MemClaw", () => {
    // A user-authored note that mentions MemClaw but isn't our exact
    // paragraph must NOT be touched.
    const content =
      "# Heartbeat\n\n- Note: MemClaw is the persistent memory backend.\n";
    const result = cleanupStaleHeartbeatEducation(content);
    assert.equal(result.cleaned, false);
    assert.equal(result.content, content);
  });

  test("strips multiple stale paragraphs accumulated across reinstalls (single pass)", () => {
    // Wet-test on openclaw-test-ran (CAURA-333) found HEARTBEAT.md
    // files containing TWO DEFAULT_EDUCATION paragraphs — N=13 from
    // a 0.98.5 install, N=9 from a 1.x reinstall after `.educated`
    // was cleared. Pre-fix the cleanup stripped one per call and
    // left a stray `---` rule. The looped helper now strips both
    // (and any future N-paragraph accumulation) cleanly.
    const userContent =
      "# HEARTBEAT.md\n\n" +
      "# Keep this file empty (or with only comments) to skip heartbeat API calls.\n\n" +
      "# Add tasks below when you want the agent to check something periodically.";
    const para = (n: number) =>
      "You have been connected to MemClaw — a shared persistent memory system. " +
      `You now have ${n} tools for writing, searching, and managing memories. ` +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.";
    const content =
      userContent +
      "\n\n---\n\n" + para(13) + "\n" +
      "\n---\n\n" + para(9) + "\n";

    const result = cleanupStaleHeartbeatEducation(content);

    assert.equal(result.cleaned, true);
    assert.equal(
      result.content,
      userContent,
      "BOTH paragraphs and BOTH separators must be stripped in a single call",
    );
    assert.ok(
      !result.content.includes("---"),
      "no stray `---` rule should remain after stripping multiple paragraphs",
    );
  });

  test("strips three accumulated paragraphs (no upper bound on cleanup count)", () => {
    const para = (n: number) =>
      "You have been connected to MemClaw — a shared persistent memory system. " +
      `You now have ${n} tools for writing, searching, and managing memories. ` +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.";
    const content =
      "user note\n\n---\n\n" + para(7) + "\n" +
      "\n---\n\n" + para(13) + "\n" +
      "\n---\n\n" + para(9) + "\n";

    const result = cleanupStaleHeartbeatEducation(content);

    assert.equal(result.cleaned, true);
    assert.equal(result.content, "user note");
  });
});

describe("writeEducationFiles → HEARTBEAT.md cleanup (integration)", () => {
  const dirs: string[] = [];
  function tmpBase(): string {
    const d = makeTmpBase();
    dirs.push(d);
    return d;
  }
  afterEach(() => {
    for (const d of dirs) {
      try { rmSync(d, { recursive: true, force: true }); } catch {}
    }
    dirs.length = 0;
  });

  test("strips legacy DEFAULT_EDUCATION paragraph from existing HEARTBEAT.md", () => {
    const base = tmpBase();
    const wsDir = join(base, "workspace");
    mkdirSync(wsDir, { recursive: true });

    const userContent =
      "# Heartbeat tasks\n\n1. Run nightly report\n2. Sync skills\n";
    const stale =
      "\n\n---\n\nYou have been connected to MemClaw — a shared persistent memory system. " +
      "You now have 9 tools for writing, searching, and managing memories. " +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.\n";
    writeFileSync(join(wsDir, "HEARTBEAT.md"), userContent + stale, "utf-8");

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    const after = readFileSync(join(wsDir, "HEARTBEAT.md"), "utf-8");
    assert.equal(after, userContent, "stale paragraph + separator must be stripped, user content preserved");
  });

  test("does NOT touch HEARTBEAT.md when paragraph is absent", () => {
    const base = tmpBase();
    const wsDir = join(base, "workspace");
    mkdirSync(wsDir, { recursive: true });

    const userContent = "# My heartbeat\n\nNo MemClaw paragraph here.\n";
    const hbPath = join(wsDir, "HEARTBEAT.md");
    writeFileSync(hbPath, userContent, "utf-8");
    const mtimeBefore = statSync(hbPath).mtimeMs;

    // Tiny wait to make a write detectable via mtime.
    const start = Date.now();
    while (Date.now() - start < 5) { /* spin */ }

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    const after = readFileSync(hbPath, "utf-8");
    assert.equal(after, userContent, "content unchanged");
    const mtimeAfter = statSync(hbPath).mtimeMs;
    assert.equal(mtimeAfter, mtimeBefore, "no spurious write when no cleanup needed");
  });

  test("missing HEARTBEAT.md is a no-op (does not create one)", () => {
    const base = tmpBase();
    const wsDir = join(base, "workspace");
    mkdirSync(wsDir, { recursive: true });

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.ok(
      !existsSync(join(wsDir, "HEARTBEAT.md")),
      "writeEducationFiles must not create HEARTBEAT.md",
    );
  });

  test("unlinks HEARTBEAT.md when stripping leaves it empty (first-install case)", () => {
    // Pre-fix the cleanup wrote a 0-byte HEARTBEAT.md when the file
    // had only the legacy paragraph. Now we unlink instead — the file
    // existed solely because of our pre-C1 write, and educateAgents
    // treats absent and empty identically when the host pushes a
    // prompt later.
    const base = tmpBase();
    const wsDir = join(base, "workspace");
    mkdirSync(wsDir, { recursive: true });

    const onlyParagraph =
      "You have been connected to MemClaw — a shared persistent memory system. " +
      "You now have 9 tools for writing, searching, and managing memories. " +
      "Check skills/memclaw/SKILL.md for full instructions. Key rules: always search before " +
      "starting work, always write findings after completing work, always include your agent_id.\n";
    const hbPath = join(wsDir, "HEARTBEAT.md");
    writeFileSync(hbPath, onlyParagraph, "utf-8");

    writeEducationFiles(buildToolsMd(), buildAgentsMd(), undefined, base);

    assert.ok(
      !existsSync(hbPath),
      "HEARTBEAT.md should be unlinked, not left as a 0-byte file",
    );
  });
});

// ── Education file builder contracts ──
//
// These assertions encode role separation and transport-neutrality
// invariants. If any fail, the education content has drifted from the
// design captured in the comment block above the builders in educate.ts.

/** Host-specific words that must not appear in agent-facing education content. */
const FORBIDDEN_HOST_TERMS = ["plugin", "gateway", "openclaw"];

function assertTransportNeutral(content: string, label: string): void {
  const lower = content.toLowerCase();
  for (const term of FORBIDDEN_HOST_TERMS) {
    assert.ok(
      !lower.includes(term),
      `${label} leaked host-specific term "${term}"`,
    );
  }
}

describe("shared SKILL.md (plugin/skills/memclaw/SKILL.md)", () => {
  // SKILL.md is no longer generated by a builder. It ships as a static file
  // at the plugin root (`plugin/skills/memclaw/SKILL.md`), discovered by
  // OpenClaw via `openclaw.plugin.json:skills`. These tests pin the file's
  // presence and the invariants the content must hold.

  test("static file exists at the expected plugin-root path", () => {
    assert.ok(
      existsSync(SHARED_SKILL_PATH),
      `expected shared skill file at ${SHARED_SKILL_PATH}`,
    );
  });

  test("has OpenClaw-required frontmatter keys (name, description)", () => {
    const skill = readSharedSkill();
    assert.ok(skill.startsWith("---\n"), "missing YAML frontmatter delimiter");
    assert.ok(/\nname:\s*memclaw\b/.test(skill), "missing/wrong name: memclaw");
    assert.ok(/\ndescription:\s*\S/.test(skill), "missing description");
  });

  test("suppresses slash-command exposure (user-invocable: false)", () => {
    const skill = readSharedSkill();
    assert.ok(
      /\nuser-invocable:\s*false\b/.test(skill),
      "shared skill should set user-invocable: false to avoid a /memclaw slash command",
    );
  });

  test("gates on plugins.entries.memclaw.enabled via requires.config", () => {
    const skill = readSharedSkill();
    assert.ok(
      skill.includes("plugins.entries.memclaw.enabled"),
      "shared skill should be gated on plugin-enabled config path",
    );
    // metadata must be a single-line JSON object per OpenClaw's parser
    const metadataMatch = skill.match(/\nmetadata:\s*(\{.*\})\s*\n/);
    assert.ok(metadataMatch, "metadata frontmatter must be present and single-line");
    assert.doesNotThrow(
      () => JSON.parse(metadataMatch![1]),
      "metadata must be valid JSON",
    );
  });

  test("required body sections are present", () => {
    const skill = readSharedSkill();
    assert.ok(skill.includes("## Your identity"), "missing identity section");
    assert.ok(skill.includes("`agent_id`"), "missing agent_id");
    assert.ok(skill.includes("`fleet_id`"), "missing fleet_id");
    assert.ok(skill.includes("## The three rules"), "missing three rules");
    assert.ok(skill.includes("## Trust levels"), "missing trust section");
    assert.ok(skill.includes("## Sharing"), "missing sharing section");
    assert.ok(skill.includes("## Session loop"), "missing session loop");
  });

  test("identity section uses MUST language", () => {
    const skill = readSharedSkill();
    const idx = skill.indexOf("## Your identity");
    assert.ok(idx >= 0);
    const section = skill.slice(idx, skill.indexOf("## The three rules"));
    assert.ok(section.includes("MUST"), "identity section lacks MUST language");
  });

  test("Rule 3 describes delete as soft-delete requiring trust 3", () => {
    const skill = readSharedSkill();
    assert.ok(skill.includes("soft-delete"), "Rule 3 should say soft-delete");
    assert.ok(skill.includes("trust 3"), "Rule 3 should state trust level 3");
    assert.ok(
      !skill.toLowerCase().includes("hard delete") &&
        !skill.toLowerCase().includes("hard-delete"),
      "SKILL.md must not describe delete as hard-delete",
    );
  });

  test("holds the deep-dive tool reference relocated from TOOLS.md", () => {
    // The tool cards, decision tree, constraints, and error codes were moved
    // out of the bootstrap-every-turn TOOLS.md into this on-demand file as
    // part of the per-turn token-footprint reduction. Each section must land
    // here or the model loses the reference entirely.
    const skill = readSharedSkill();
    assert.ok(skill.includes("## Tool reference"), "missing ## Tool reference section");
    assert.ok(skill.includes("### Tool cards"), "missing ### Tool cards");
    assert.ok(skill.includes("### Which tool, when"), "missing ### Which tool, when");
    assert.ok(skill.includes("### Constraints that matter"), "missing ### Constraints that matter");
    assert.ok(skill.includes("### Error codes"), "missing ### Error codes");
  });

  test("all 10 tool cards are present in SKILL.md", () => {
    const skill = readSharedSkill();
    for (const tool of [
      "memclaw_recall", "memclaw_write", "memclaw_manage", "memclaw_list",
      "memclaw_doc", "memclaw_entity_get", "memclaw_tune",
      "memclaw_insights", "memclaw_evolve", "memclaw_stats",
    ]) {
      assert.ok(
        skill.includes(`**\`${tool}(`) || skill.includes(`\`${tool}(`),
        `SKILL.md missing tool card for ${tool}`,
      );
    }
  });

  test("error codes appear verbatim in SKILL.md", () => {
    const skill = readSharedSkill();
    for (const code of ["INVALID_ARGUMENTS", "BATCH_TOO_LARGE", "INVALID_BATCH_ITEM"]) {
      assert.ok(skill.includes(code), `SKILL.md missing error code ${code}`);
    }
  });

  // Note: the shared SKILL.md is intentionally a plugin artifact — it is
  // gated on `plugins.entries.memclaw.enabled` and its footer references
  // the plugin install path. The transport-neutrality check does NOT apply
  // here (it still applies to TOOLS.md / AGENTS.md, which append to
  // workspace-owned files that are loaded across transports).
});

describe("buildToolsMd", () => {
  const ALL_TOOLS = [
    "memclaw_recall",
    "memclaw_write",
    "memclaw_manage",
    "memclaw_list",
    "memclaw_doc",
    "memclaw_entity_get",
    "memclaw_tune",
    "memclaw_insights",
    "memclaw_evolve",
    "memclaw_stats",
  ];

  test("lists every plugin-exposed tool", () => {
    const tools = buildToolsMd();
    for (const tool of ALL_TOOLS) {
      assert.ok(tools.includes(tool), `missing tool: ${tool}`);
    }
  });

  test("contains exactly the lean sections (Quick Matrix + Vocabulary)", () => {
    // TOOLS.md is intentionally lean: it ships only the quick matrix (so the
    // model knows what tools exist each turn) and the vocabulary table (so
    // sub-agents — which receive only AGENTS.md and TOOLS.md — still have
    // enum values without needing to read SKILL.md). Everything else (tool
    // cards, decision tree, constraints, error codes) lives in SKILL.md.
    const tools = buildToolsMd();
    assert.ok(tools.includes("### Quick matrix"), "missing quick matrix");
    assert.ok(tools.includes("### Vocabulary"), "missing vocabulary");
    // Deep-dive sections must NOT appear in TOOLS.md — they were relocated
    // to SKILL.md as part of the per-turn token-footprint reduction.
    assert.ok(!tools.includes("### Tool cards"), "tool cards must live in SKILL.md, not TOOLS.md");
    assert.ok(!tools.includes("### Which tool, when"), "decision tree must live in SKILL.md, not TOOLS.md");
    assert.ok(!tools.includes("### Constraints"), "constraints must live in SKILL.md, not TOOLS.md");
    assert.ok(!tools.includes("### Error codes"), "error codes must live in SKILL.md, not TOOLS.md");
  });

  test("points readers to SKILL.md for the deep dive", () => {
    const tools = buildToolsMd();
    assert.ok(
      tools.includes("skills/memclaw/SKILL.md"),
      "TOOLS.md must point readers at SKILL.md for per-tool signatures and error codes",
    );
  });

  test("vocabulary covers every memory_type from the SoT (drift guard)", () => {
    // If this fails, either MEMORY_TYPES in tool-definitions.ts changed (update
    // TOOLS.md), or someone trimmed the TOOLS.md vocabulary row (don't — the
    // model needs the full enumeration).
    const tools = buildToolsMd();
    for (const t of MEMORY_TYPES) {
      assert.ok(tools.includes(t), `vocabulary missing memory_type "${t}"`);
    }
  });

  test("vocabulary covers every status from the SoT (drift guard)", () => {
    const tools = buildToolsMd();
    for (const s of STATUSES) {
      assert.ok(tools.includes(s), `vocabulary missing status "${s}"`);
    }
  });

  test("vocabulary covers the remaining schema-only enums", () => {
    const tools = buildToolsMd();
    for (const v of ["scope_agent", "scope_team", "scope_org"]) {
      assert.ok(tools.includes(v), `vocabulary missing visibility "${v}"`);
    }
    for (const m of ["fast", "strong"]) {
      assert.ok(tools.includes(m), `vocabulary missing write_mode "${m}"`);
    }
    for (const f of ["contradictions", "divergence", "discover"]) {
      assert.ok(tools.includes(f), `vocabulary missing focus "${f}"`);
    }
  });

  test("vocabulary has a fleet_ids row (recall cross-fleet filter)", () => {
    const tools = buildToolsMd();
    assert.ok(
      tools.includes("`fleet_ids`"),
      "vocabulary missing fleet_ids entry",
    );
  });

  test("quick-matrix header uses dynamic tool count", () => {
    const tools = buildToolsMd();
    assert.ok(
      tools.includes(`### Quick matrix · ${MEMCLAW_TOOLS.length} tools`),
      `quick matrix header missing dynamic tool count (expected "### Quick matrix · ${MEMCLAW_TOOLS.length} tools")`,
    );
  });

  test("is transport-neutral", () => {
    assertTransportNeutral(buildToolsMd(), "TOOLS.md");
  });
});

describe("buildAgentsMd", () => {
  test("contains the '## Memory V2' idempotency anchor", () => {
    // writeEducationFiles() skips append when the workspace AGENTS.md
    // already contains "## Memory V2". Breaking this anchor would cause
    // duplicate appends on re-education.
    const agents = buildAgentsMd();
    assert.ok(
      agents.includes("## Memory V2"),
      "AGENTS.md section lost the '## Memory V2' anchor; writeEducationFiles idempotency will break",
    );
  });

  test("carries identity mandate (slim form)", () => {
    // Post-slim AGENTS.md: identity is a one-paragraph mandate, not a
    // dedicated H3 section. The deep identity reference lives in
    // SKILL.md ("Your identity: agent_id and fleet_id"). What remains
    // here MUST still name both ids and assert the MUST.
    const agents = buildAgentsMd();
    assert.ok(/\*\*Identity\.\*\*/.test(agents), "missing **Identity.** lead-in");
    assert.ok(agents.includes("`agent_id`"));
    assert.ok(agents.includes("`fleet_id`"));
    assert.ok(agents.includes("MUST"));
  });

  test("write triggers are present as a single inline line", () => {
    // Post-slim: triggers are intentionally one compact line in
    // AGENTS.md (the verbose bullet list lives in SKILL.md). The
    // canonical short-form triggers below MUST all appear.
    const agents = buildAgentsMd();
    assert.ok(/\*\*Write triggers\.\*\*/.test(agents));
    for (const trigger of [
      "Task done",
      "bug",
      "deploy",
      "decision",
      "API change",
      "blocker",
      "commitment",
      "config change",
      "error pattern",
    ]) {
      assert.ok(agents.includes(trigger), `missing trigger token: ${trigger}`);
    }
  });

  test("is slim: under 1700 chars (deep content lives in SKILL.md)", () => {
    // Post-slim contract: per-turn injection cost should be small. The
    // pre-slim version was ~3.4 KB. If this grows back, ask whether
    // the new content really has to be every-turn and not on-demand.
    //
    // Limit history:
    //  - 1500 (original A3 slim)
    //  - 1700 (CAURA-444: add Skills paragraph + recall-auto-gate cue)
    //
    // Bump deliberately when adding a new genuinely-must-be-every-turn
    // cue; never to absorb verbose content that belongs in SKILL.md.
    const agents = buildAgentsMd();
    assert.ok(
      agents.length < 1700,
      `buildAgentsMd is ${agents.length} chars; should be < 1700. Move deep content to SKILL.md.`,
    );
  });

  test("delete-prohibition framing lives in SKILL.md, not AGENTS.md", () => {
    // We moved prohibitions to SKILL.md so they're not duplicated every
    // turn. AGENTS.md must NOT call op=delete a hard-delete (regression
    // guard from the pre-slim era).
    const agents = buildAgentsMd();
    assert.ok(
      !agents.toLowerCase().includes("hard-delete"),
      "AGENTS.md must not call op=delete a hard-delete",
    );

    const skill = readFileSync(SHARED_SKILL_PATH, "utf-8");
    assert.ok(
      skill.includes("NEVER delete memories you merely disagree with"),
      "SKILL.md must carry the 'merely disagree' framing for op=delete",
    );
    assert.ok(
      skill.includes("soft-delete"),
      "SKILL.md must clarify op=delete is a soft-delete",
    );
    assert.ok(
      !skill.toLowerCase().includes("hard-delete"),
      "SKILL.md must not call op=delete a hard-delete",
    );
  });

  test("3-layer capture, subagent protocol, and prohibitions live in SKILL.md", () => {
    // Deep content was moved out of AGENTS.md (every-turn) into SKILL.md
    // (on-demand). This test guards against the move regressing.
    const skill = readFileSync(SHARED_SKILL_PATH, "utf-8");
    assert.ok(skill.includes("Capture cadence"), "SKILL.md missing L1/L2/L3 capture cadence");
    assert.ok(skill.includes("L1 — per turn"));
    assert.ok(skill.includes("L2 — session boundary"));
    assert.ok(skill.includes("L3 — consolidation"));
    assert.ok(
      skill.includes("Orchestrator + subagent"),
      "SKILL.md missing orchestrator/subagent protocol",
    );
    assert.ok(skill.includes("Prohibitions"), "SKILL.md missing prohibitions section");
    assert.ok(skill.includes("NEVER fabricate"));
    assert.ok(skill.includes("NEVER silently drop"));
  });

  test("nudges the model to read SKILL.md before the first MemClaw call", () => {
    // The tool-reference deep dive lives in SKILL.md (loaded via `read` on
    // demand). AGENTS.md — injected as bootstrap every turn — must direct
    // the model to load SKILL.md before its first MemClaw tool call in a
    // session so the signatures, decision guidance, and error codes are
    // in context when needed.
    const agents = buildAgentsMd();
    assert.ok(
      agents.includes("skills/memclaw/SKILL.md"),
      "AGENTS.md must reference skills/memclaw/SKILL.md by path",
    );
    assert.ok(
      /before your first MemClaw/i.test(agents),
      "AGENTS.md must carry a 'before your first MemClaw call' nudge",
    );
  });

  test("is transport-neutral", () => {
    assertTransportNeutral(buildAgentsMd(), "AGENTS.md");
  });
});

describe("openclaw.plugin.json manifest", () => {
  // OpenClaw's plugin-skill resolver discovers SKILL.md via the `skills`
  // field in the manifest. If this field is dropped or the path is wrong,
  // the shared skill is invisible to agents even though the file exists.

  const MANIFEST_PATH = join(__dirname, "..", "openclaw.plugin.json");

  test("declares skills: [\"skills\"] so OpenClaw's resolver finds the shared SKILL.md", () => {
    assert.ok(existsSync(MANIFEST_PATH), `manifest not found at ${MANIFEST_PATH}`);
    const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf-8"));
    assert.ok(Array.isArray(manifest.skills), "manifest missing `skills` array");
    assert.ok(
      manifest.skills.includes("skills"),
      `manifest.skills should contain "skills" (got ${JSON.stringify(manifest.skills)})`,
    );
  });
});
