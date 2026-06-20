import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, readFileSync, mkdtempSync, mkdirSync, rmSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";
import {
  autoFixAllowlist,
  ensureExtraSkillDirs,
  isContextEngineSlotClaimed,
  isMemclawAllowed,
  isMemclawFullyConfigured,
  isMemorySlotClaimed,
  shouldRunAutoFix,
} from "./config.js";
import { getPluginDir } from "./paths.js";

// Minimal "happy-path" config scaffold — every predicate true. Individual
// tests selectively break one field at a time.
function happyConfig(): Record<string, unknown> {
  return {
    plugins: {
      allow: ["memclaw"],
      entries: { memclaw: { enabled: true } },
      load: { paths: [getPluginDir()] },
      slots: { memory: "memclaw", contextEngine: "memclaw" },
    },
    tools: { alsoAllow: [] },
  };
}

describe("isMemorySlotClaimed", () => {
  test("false when plugins.slots is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots;
    assert.equal(isMemorySlotClaimed(c), false);
  });

  test("false when memory slot is held by a different plugin", () => {
    const c = happyConfig();
    (c as any).plugins.slots.memory = "memory-core";
    assert.equal(isMemorySlotClaimed(c), false);
  });

  test("true when memory slot is memclaw", () => {
    assert.equal(isMemorySlotClaimed(happyConfig()), true);
  });
});

describe("isMemclawFullyConfigured", () => {
  // Paints the Fleet UI dashboard via heartbeat.setup_status.fully_configured.
  // Each test below corresponds to one of the four conditions that must hold.

  test("true on happy-path config", () => {
    assert.equal(isMemclawFullyConfigured(happyConfig()), true);
  });

  test("false when memclaw is not in a restrictive allowlist", () => {
    // CAURA-000: pre-fix this test used `plugins.allow = []` to mean
    // "not allowlisted", which assumed empty = restrictive. The
    // OpenClaw runtime actually treats empty (and missing) as
    // PERMISSIVE — "no restriction". Use an explicit non-empty
    // allowlist that excludes memclaw to express the real
    // "not allowlisted" case.
    const c = happyConfig();
    (c as any).plugins.allow = ["some-other-plugin"];
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when memclaw is disabled", () => {
    const c = happyConfig();
    (c as any).plugins.entries.memclaw.enabled = false;
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when plugin path is not loaded", () => {
    const c = happyConfig();
    (c as any).plugins.load.paths = [];
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when memory slot is not claimed", () => {
    const c = happyConfig();
    (c as any).plugins.slots.memory = "memory-core";
    assert.equal(isMemclawFullyConfigured(c), false);
  });
});


describe("isContextEngineSlotClaimed (CAURA-000 — keystone-injection gate)", () => {
  // OpenClaw 2026.5.4 dist/registry-DFFgCbcm.js:241 resolveContextEngine
  // reads config.plugins.slots.contextEngine. Without it set to "memclaw",
  // OpenClaw uses its default "legacy" engine and our assemble() is never
  // called — so the <keystone_rules> block never reaches the prompt.

  test("false when plugins.slots is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots;
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("false when contextEngine slot held by another plugin (e.g. legacy)", () => {
    const c = happyConfig();
    (c as any).plugins.slots.contextEngine = "legacy";
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("false when contextEngine slot is undefined (the WhatsApp-regression case)", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots.contextEngine;
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("true when contextEngine slot is memclaw", () => {
    assert.equal(isContextEngineSlotClaimed(happyConfig()), true);
  });
});

describe("isMemclawFullyConfigured — contextEngine slot is now required", () => {
  // Pre-fix happyConfig() didn't include contextEngine and isMemclawFullyConfigured
  // returned true anyway. That hid the WhatsApp keystone-injection regression
  // because Fleet UI's "fully configured" badge was green while assemble()
  // silently never ran. Adding the slot to the predicate surfaces the gap.

  test("false when contextEngine slot is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots.contextEngine;
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when contextEngine slot is held by another plugin", () => {
    const c = happyConfig();
    (c as any).plugins.slots.contextEngine = "legacy";
    assert.equal(isMemclawFullyConfigured(c), false);
  });
});

describe("shouldRunAutoFix — allowlist drift gate", () => {
  // The original gate ran auto-fix once (guarded by .allowlist-applied),
  // so a plugin upgrade that ADDED a tool (memclaw_keystones) never landed
  // it in tools.alsoAllow on existing installs — and a later OpenClaw
  // tools.profile then stripped it. The gate now also re-runs on drift.
  const clean = {
    flagExists: true,
    missingToolCount: 0,
    contextEngineSlotClaimed: true,
  };

  test("MEMCLAW_AUTO_FIX_CONFIG=true always runs (explicit force)", () => {
    assert.equal(shouldRunAutoFix({ ...clean, autoFixEnv: "true" }), true);
  });

  test("MEMCLAW_AUTO_FIX_CONFIG=false never runs, even with drift", () => {
    assert.equal(
      shouldRunAutoFix({
        autoFixEnv: "false",
        flagExists: false,
        missingToolCount: 5,
        contextEngineSlotClaimed: false,
      }),
      false,
    );
  });

  test("first run (no flag) runs", () => {
    assert.equal(shouldRunAutoFix({ ...clean, flagExists: false }), true);
  });

  test("re-runs when a tool is missing despite the flag (the keystones upgrade case)", () => {
    assert.equal(shouldRunAutoFix({ ...clean, missingToolCount: 1 }), true);
  });

  test("re-runs when the contextEngine slot is unclaimed despite the flag", () => {
    assert.equal(
      shouldRunAutoFix({ ...clean, contextEngineSlotClaimed: false }),
      true,
    );
  });

  test("no-ops on a clean install with the flag present", () => {
    assert.equal(shouldRunAutoFix(clean), false);
  });
});


// ---- isMemclawAllowed — permissive allowlist semantics (CAURA-000) ----
//
// OpenClaw 2026.6.x treats `plugins.allow` as a STRICT allowlist only when
// it is BOTH present AND non-empty. A missing or empty array means "no
// restriction" — every enabled plugin can load. Pre-fix our predicate
// reported "not allowed" for the empty/missing case, which caused
// `autoFixAllowlist` to *create* `["memclaw"]` — silently converting a
// permissive config into a restrictive one and locking out built-ins
// like the bundled `openai` provider plugin.

describe("isMemclawAllowed — permissive when allow is missing or empty (CAURA-000)", () => {
  test("true when plugins.allow is missing entirely (permissive default)", () => {
    const c: any = { plugins: { entries: {}, load: {}, slots: {} } };
    assert.equal(isMemclawAllowed(c), true);
  });

  test("true when plugins.allow is an empty array (permissive)", () => {
    const c: any = { plugins: { allow: [], entries: {}, load: {}, slots: {} } };
    assert.equal(isMemclawAllowed(c), true);
  });

  test("true when plugins.allow is non-empty AND includes memclaw", () => {
    const c: any = {
      plugins: { allow: ["memclaw", "browser"], entries: {}, load: {}, slots: {} },
    };
    assert.equal(isMemclawAllowed(c), true);
  });

  test("false when plugins.allow is non-empty AND excludes memclaw (the only real 'not allowed' case)", () => {
    const c: any = {
      plugins: { allow: ["browser"], entries: {}, load: {}, slots: {} },
    };
    assert.equal(isMemclawAllowed(c), false);
  });

  test("true when plugins object is completely missing (no allowlist to speak of)", () => {
    const c: any = {};
    assert.equal(isMemclawAllowed(c), true);
  });
});


// ---- autoFixAllowlist.plugins.allow — non-creation invariant (CAURA-000) ----
//
// On a fresh install, autoFixAllowlist must NOT create `plugins.allow` from
// nothing. Doing so would silently flip the user's permissive OpenClaw
// config into a restrictive one — the exact mechanism behind the
// customer-reported "openai disabled after 2.8.1 install" symptom.
//
// These tests drive the real `autoFixAllowlist` function against tiny
// temp `openclaw.json` files and read back the resulting config.

function _autoFixWithConfig(initial: Record<string, unknown>): {
  written: Record<string, unknown> | null;
  result: ReturnType<typeof autoFixAllowlist>;
  cleanup: () => void;
} {
  const tmp = mkdtempSync(join(tmpdir(), "memclaw-autofix-test-"));
  mkdirSync(join(tmp, ".openclaw"), { recursive: true });
  const cfgPath = join(tmp, ".openclaw", "openclaw.json");
  writeFileSync(cfgPath, JSON.stringify(initial), "utf-8");
  const prevHome = process.env.HOME;
  process.env.HOME = tmp;
  const result = autoFixAllowlist({ forceSlotOverride: false });
  process.env.HOME = prevHome;
  let written: Record<string, unknown> | null = null;
  try {
    written = JSON.parse(readFileSync(cfgPath, "utf-8"));
  } catch {
    written = null;
  }
  return {
    written,
    result,
    cleanup: () => {
      try {
        rmSync(tmp, { recursive: true, force: true });
      } catch {
        // best-effort
      }
    },
  };
}

describe("autoFixAllowlist — plugins.allow non-creation (CAURA-000)", () => {
  test("does NOT create plugins.allow when it was missing (fresh-install regression)", () => {
    // Mirrors a vanilla openclaw.json that the user / installer never
    // touched — no `plugins.allow` field at all. Pre-fix autoFix would
    // CREATE `plugins.allow = ["memclaw"]` here, the customer's
    // observed crash mechanism.
    const ctx = _autoFixWithConfig({
      plugins: {
        // NO `allow` field
        entries: {},
        load: { paths: [] },
        slots: {},
      },
      tools: {},
    });
    try {
      const written = ctx.written as any;
      const allow = written?.plugins?.allow;
      assert.ok(
        allow === undefined || (Array.isArray(allow) && allow.length === 0),
        `expected plugins.allow to stay missing/empty after autoFix; got: ${JSON.stringify(allow)}`,
      );
    } finally {
      ctx.cleanup();
    }
  });

  test("does NOT push to plugins.allow when it was an explicit empty array", () => {
    const ctx = _autoFixWithConfig({
      plugins: {
        allow: [], // user explicitly set empty = permissive
        entries: {},
        load: { paths: [] },
        slots: {},
      },
      tools: {},
    });
    try {
      const written = ctx.written as any;
      assert.deepEqual(written?.plugins?.allow, [], "empty allow must stay empty");
    } finally {
      ctx.cleanup();
    }
  });

  test("DOES add memclaw to an existing non-empty allowlist that excludes it", () => {
    // User has explicitly opted into a restrictive allowlist. We must
    // add memclaw to it so memclaw itself can still load — otherwise
    // we'd disable our own plugin while leaving the user's allowlist
    // semantics intact.
    const ctx = _autoFixWithConfig({
      plugins: {
        allow: ["browser", "filesystem"],
        entries: {},
        load: { paths: [] },
        slots: {},
      },
      tools: {},
    });
    try {
      const written = ctx.written as any;
      const allow: string[] = written?.plugins?.allow ?? [];
      assert.ok(
        allow.includes("memclaw"),
        `expected memclaw to be added; got: ${JSON.stringify(allow)}`,
      );
      assert.ok(allow.includes("browser"), "must preserve existing entries");
      assert.ok(allow.includes("filesystem"), "must preserve existing entries");
    } finally {
      ctx.cleanup();
    }
  });

  test("does NOT add memclaw twice when already in a non-empty allowlist", () => {
    const ctx = _autoFixWithConfig({
      plugins: {
        allow: ["browser", "memclaw", "filesystem"],
        entries: {},
        load: { paths: [] },
        slots: {},
      },
      tools: {},
    });
    try {
      const written = ctx.written as any;
      const allow: string[] = written?.plugins?.allow ?? [];
      assert.equal(
        allow.filter((p) => p === "memclaw").length,
        1,
        `memclaw must appear exactly once; got: ${JSON.stringify(allow)}`,
      );
    } finally {
      ctx.cleanup();
    }
  });
});

// ---------------------------------------------------------------------------
// ensureExtraSkillDirs — append a dir to OpenClaw's skills.load.extraDirs
// ---------------------------------------------------------------------------

function _ensureExtraDirsWithConfig(
  initial: Record<string, unknown> | null,
  dirsOrFn: string[] | ((home: string) => string[]),
): {
  written: Record<string, unknown> | null;
  result: ReturnType<typeof ensureExtraSkillDirs>;
  cleanup: () => void;
} {
  const tmp = mkdtempSync(join(tmpdir(), "memclaw-extradirs-test-"));
  mkdirSync(join(tmp, ".openclaw"), { recursive: true });
  const cfgPath = join(tmp, ".openclaw", "openclaw.json");
  if (initial !== null) {
    writeFileSync(cfgPath, JSON.stringify(initial), "utf-8");
  }
  // Resolve dirs against the SAME home the function will see, so a test can
  // build an absolute path that matches a ``~``-relative config entry.
  const dirs = typeof dirsOrFn === "function" ? dirsOrFn(tmp) : dirsOrFn;
  const prevHome = process.env.HOME;
  process.env.HOME = tmp;
  const result = ensureExtraSkillDirs(dirs);
  process.env.HOME = prevHome;
  let written: Record<string, unknown> | null = null;
  try {
    written = JSON.parse(readFileSync(cfgPath, "utf-8"));
  } catch {
    written = null;
  }
  return {
    written,
    result,
    cleanup: () => {
      try {
        rmSync(tmp, { recursive: true, force: true });
      } catch {
        // best-effort
      }
    },
  };
}

describe("ensureExtraSkillDirs", () => {
  test("creates skills.load.extraDirs and appends the dir when absent", () => {
    const ctx = _ensureExtraDirsWithConfig({ tools: {} }, ["/srv/shared/skills"]);
    try {
      assert.equal(ctx.result.changed, true);
      assert.deepEqual(ctx.result.added, ["/srv/shared/skills"]);
      const skills = ctx.written?.skills as { load?: { extraDirs?: string[] } };
      assert.deepEqual(skills.load?.extraDirs, ["/srv/shared/skills"]);
    } finally {
      ctx.cleanup();
    }
  });

  test("preserves existing extraDirs and appends only the missing one", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["/already/there"], watch: true } } },
      ["/srv/shared/skills"],
    );
    try {
      assert.equal(ctx.result.changed, true);
      assert.deepEqual(ctx.result.added, ["/srv/shared/skills"]);
      const load = (ctx.written?.skills as { load?: { extraDirs?: string[]; watch?: boolean } })
        .load;
      assert.deepEqual(load?.extraDirs, ["/already/there", "/srv/shared/skills"]);
      assert.equal(load?.watch, true, "other load keys are preserved");
    } finally {
      ctx.cleanup();
    }
  });

  test("reports alreadyPresent alongside a newly added dir", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["/already/here"] } } },
      ["/already/here", "/srv/new"],
    );
    try {
      assert.equal(ctx.result.changed, true);
      assert.deepEqual(ctx.result.added, ["/srv/new"]);
      assert.deepEqual(ctx.result.alreadyPresent, ["/already/here"]);
    } finally {
      ctx.cleanup();
    }
  });

  test("is idempotent — a dir already present is a no-op (no write)", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["/srv/shared/skills"] } } },
      ["/srv/shared/skills"],
    );
    try {
      assert.equal(ctx.result.changed, false);
      assert.deepEqual(ctx.result.added, []);
    } finally {
      ctx.cleanup();
    }
  });

  test("matches an existing ~-relative entry against an absolute dir (no dup)", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["~/shared/skills"] } } },
      (home) => [join(home, "shared", "skills")],
    );
    try {
      assert.equal(ctx.result.changed, false, "~ entry should canonically match the absolute dir");
      assert.deepEqual(ctx.result.added, []);
    } finally {
      ctx.cleanup();
    }
  });

  test("preserves non-string entries already in extraDirs (no data loss)", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["/keep/me", 42, { weird: true }] } } },
      ["/srv/shared/skills"],
    );
    try {
      assert.equal(ctx.result.changed, true);
      const load = (ctx.written?.skills as { load?: { extraDirs?: unknown[] } }).load;
      assert.deepEqual(load?.extraDirs, ["/keep/me", 42, { weird: true }, "/srv/shared/skills"]);
    } finally {
      ctx.cleanup();
    }
  });

  test("fails safe when openclaw.json is missing — error, no throw", () => {
    const ctx = _ensureExtraDirsWithConfig(null, ["/srv/shared/skills"]);
    try {
      assert.equal(ctx.result.changed, false);
      assert.ok(ctx.result.error && /not found/.test(ctx.result.error));
    } finally {
      ctx.cleanup();
    }
  });

  test("rejects a top-level JSON array — error, file NOT clobbered", () => {
    // A top-level array is truthy: a bare !config check would let it through,
    // then JSON.stringify would silently drop the .skills prop and rewrite [].
    const ctx = _ensureExtraDirsWithConfig(["/pre/existing"] as unknown as Record<string, unknown>, [
      "/srv/shared/skills",
    ]);
    try {
      assert.equal(ctx.result.changed, false);
      assert.ok(ctx.result.error && /not a JSON object/.test(ctx.result.error));
      assert.deepEqual(ctx.written, ["/pre/existing"], "original array left untouched");
    } finally {
      ctx.cleanup();
    }
  });

  test("rejects a malformed non-object skills field — error, not clobbered", () => {
    const ctx = _ensureExtraDirsWithConfig({ skills: "i am a string" }, ["/srv/shared/skills"]);
    try {
      assert.equal(ctx.result.changed, false);
      assert.ok(ctx.result.error && /'skills' is not an object/.test(ctx.result.error));
      assert.equal((ctx.written as { skills?: unknown }).skills, "i am a string", "untouched");
    } finally {
      ctx.cleanup();
    }
  });

  test("rejects a malformed non-object skills.load field — error, not clobbered", () => {
    const ctx = _ensureExtraDirsWithConfig({ skills: { load: 42 } }, ["/srv/shared/skills"]);
    try {
      assert.equal(ctx.result.changed, false);
      assert.ok(ctx.result.error && /'skills\.load' is not an object/.test(ctx.result.error));
      assert.equal((ctx.written as { skills?: { load?: unknown } }).skills?.load, 42, "untouched");
    } finally {
      ctx.cleanup();
    }
  });

  test("a relative existing entry is not falsely deduped against an absolute dir", () => {
    const ctx = _ensureExtraDirsWithConfig(
      { skills: { load: { extraDirs: ["./rel/skills"] } } },
      ["/srv/shared/skills"],
    );
    try {
      // Relative entries are compared literally (no CWD guess), so the
      // absolute dir is treated as distinct and appended; the relative
      // entry is preserved.
      assert.equal(ctx.result.changed, true);
      const load = (ctx.written?.skills as { load?: { extraDirs?: string[] } }).load;
      assert.deepEqual(load?.extraDirs, ["./rel/skills", "/srv/shared/skills"]);
    } finally {
      ctx.cleanup();
    }
  });

  test("empty input is a no-op", () => {
    const ctx = _ensureExtraDirsWithConfig({ skills: {} }, []);
    try {
      assert.equal(ctx.result.changed, false);
      assert.deepEqual(ctx.result.added, []);
    } finally {
      ctx.cleanup();
    }
  });
});
