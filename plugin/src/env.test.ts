/**
 * Tests for tenant-id resolution behavior in env.ts.
 *
 * Guards the OSS-noise fix: when the backend is unreachable (undici throws
 * TypeError("fetch failed")), the resolver must short-circuit with one warn
 * line instead of 4 retries over ~14s.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

// Set API key before importing env.ts so resolveTenantId won't early-exit.
// Must be MEMCLAW_*-prefixed — env.ts only loads those from .env.
process.env.MEMCLAW_API_KEY = "mc_test_key_for_env_tests";
// Clear tenant id so resolveTenantId actually attempts a fetch.
delete process.env.MEMCLAW_TENANT_ID;

const { resolveTenantId, readEnv, isPluginEnvKey, hasPluginEnvPrefix } = await import("./env.js");

interface MockCall {
  url: string;
  init?: RequestInit;
}

let originalFetch: typeof fetch;
let calls: MockCall[];
let warnLines: string[];
let errorLines: string[];
let originalWarn: typeof console.warn;
let originalError: typeof console.error;

function installConsoleCapture(): void {
  warnLines = [];
  errorLines = [];
  originalWarn = console.warn;
  originalError = console.error;
  console.warn = (...args: unknown[]) => {
    warnLines.push(args.map((a) => String(a)).join(" "));
  };
  console.error = (...args: unknown[]) => {
    errorLines.push(args.map((a) => String(a)).join(" "));
  };
}

function restoreConsole(): void {
  console.warn = originalWarn;
  console.error = originalError;
}

describe("resolveTenantId — network failure handling", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    installConsoleCapture();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    restoreConsole();
  });

  test("short-circuits on TypeError('fetch failed') — one log line, no retry backoff", async () => {
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      // undici throws this shape for DNS/ECONNREFUSED/TLS failures
      throw new TypeError("fetch failed");
    }) as typeof fetch;

    const t0 = Date.now();
    const result = await resolveTenantId();
    const elapsed = Date.now() - t0;

    assert.equal(result, "", "returns empty string on failure");
    assert.equal(calls.length, 1, "should only attempt fetch once (no retries)");
    assert.ok(elapsed < 500, `should short-circuit fast, took ${elapsed}ms`);
    assert.equal(
      warnLines.length,
      1,
      `expected 1 warn line, got ${warnLines.length}: ${warnLines.join(" | ")}`,
    );
    assert.match(warnLines[0], /tenant_id resolution skipped/);
    assert.match(warnLines[0], /standalone mode/);
    assert.equal(errorLines.length, 0, "no error-level output for network failures");
  });

  test("passes an AbortSignal to fetch on every attempt (bounds per-attempt latency — CAURA-000)", async () => {
    // Pins the contract that the ``/auth/verify`` fetch in
    // ``resolveTenantId`` MUST be invoked with an AbortSignal — without
    // it, a backend that accepts the TCP connection but never replies
    // hangs ``ensureTenantId`` forever, which in turn stalls every
    // lifecycle hook (ingest / assemble / afterTurn) sitting behind the
    // memoized ``_tenantPromise``. Observed downstream in a customer
    // install as an OpenClaw ``stalled_agent_run`` diagnostic
    // (``embedded_run age=156s, queueDepth=4``). A structural assertion
    // is enough here — we don't need to wait for the actual timeout to
    // fire, just verify the signal is present and reaches the fetch on
    // every retry attempt.
    let signalsCount = 0;
    globalThis.fetch = (async (_input: string | URL | Request, init?: RequestInit) => {
      if (init?.signal instanceof AbortSignal) signalsCount++;
      // Short-circuit via TypeError so the test doesn't loop through
      // the full retry backoff.
      throw new TypeError("fetch failed");
    }) as typeof fetch;

    await resolveTenantId();

    assert.equal(
      signalsCount,
      1,
      "fetch must be invoked with an AbortSignal on every attempt — got " +
        `${signalsCount} signal(s) on 1 attempt`,
    );
  });

  test("non-TypeError errors still follow the retry path (preserves 5xx/timeout behavior)", async () => {
    // Use a non-TypeError Error to exercise the else branch. We stub
    // setTimeout to fire immediately, collapsing the 14s backoff into
    // zero real time so the retry loop completes in microseconds.
    const originalSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = ((cb: () => void) => {
      // Fire immediately — ignore the delay. Return a dummy timer handle.
      Promise.resolve().then(cb);
      return 0 as unknown as ReturnType<typeof originalSetTimeout>;
    }) as unknown as typeof setTimeout;

    let callCount = 0;
    globalThis.fetch = (async () => {
      callCount++;
      throw new Error("socket timeout"); // plain Error, not TypeError
    }) as typeof fetch;

    try {
      const result = await resolveTenantId();
      assert.equal(result, "", "returns empty after all retries exhausted");
      assert.equal(callCount, 4, `expected 4 attempts (initial + 3 retries), got ${callCount}`);
      assert.equal(
        warnLines.filter((l) => /attempt \d+\/4 failed: socket timeout/.test(l)).length,
        3,
        `expected 3 retry-warn lines (attempts 1-3), got: ${warnLines.join(" | ")}`,
      );
      assert.ok(
        errorLines.some((l) => /failed after 4 attempts: socket timeout/.test(l)),
        `expected final error line, got: ${errorLines.join(" | ")}`,
      );
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
  });
});

// --- Default-value source-pin tests ---
//
// We don't pin defaults via runtime import because other test files in
// this suite override env vars at their module-load time (e.g.
// ``keystones.test.ts:26`` sets ``MEMCLAW_KEYSTONES_TOKEN_CAP=120``),
// and ``node --test`` shares process env across files — so any
// runtime read could see a polluted value. Reading the source TS
// file's literal value is robust to that, and also documents the
// chosen default in a way that fails CI loudly if someone bumps it
// without intent.

describe("env.ts default-value source pins", () => {
  test("MEMCLAW_KEYSTONES_TOKEN_CAP default literal is 1500 (CAURA-000)", async () => {
    const { readFile } = await import("node:fs/promises");
    const { fileURLToPath } = await import("node:url");
    const { dirname, join } = await import("node:path");
    // The dist/env.test.js sits next to dist/env.js but we need the
    // SOURCE env.ts. Resolve via project structure: dist/env.test.js
    // → plugin/dist/ → plugin/src/env.ts.
    const here = dirname(fileURLToPath(import.meta.url));
    const envSrc = await readFile(join(here, "..", "src", "env.ts"), "utf8");
    // Match the exact ``_readIntEnv`` call site to avoid colliding with
    // doc-comment occurrences of the number 1500.
    // Tolerates the ratchet's ``legacy-name-ok`` comment between the alias
    // array and the default, which is where it has to sit.
    const re =
      /_readIntEnv\(\s*\[[^\]]*"MEMCLAW_KEYSTONES_TOKEN_CAP"[^\]]*\]\s*,(?:\s*\/\/[^\n]*)?\s*(\d+)\s*,/;  // legacy-name-ok: rule 3 dual-read alias
    const match = envSrc.match(re);
    assert.ok(
      match,
      "could not locate MEMCLAW_KEYSTONES_TOKEN_CAP _readIntEnv call in env.ts",
    );
    assert.equal(
      match![1],
      "1500",
      "MEMCLAW_KEYSTONES_TOKEN_CAP default must be 1500 — bumped from 500 " +
        "after CAURA-000 customer with 16 rules saw 4 dropped at every turn. " +
        "If you intend to change it, also update the doc comment in env.ts " +
        "and review the keystones formatter's truncation behavior in " +
        "keystones.test.ts.",
    );
  });
});

// --- Environment dual-read (Phase 5.1) ---

describe("isPluginEnvKey — .env allow-list", () => {
  test("accepts both prefixes", () => {
    assert.ok(isPluginEnvKey("CAURA_API_KEY"), "CAURA_* must not be dropped");
    assert.ok(isPluginEnvKey("MEMCLAW_API_KEY"), "old prefix keeps working"); // legacy-name-ok: rule 3 — pins that the old prefix is still accepted
  });

  test("is stricter than hasPluginEnvPrefix, deliberately", () => {
    // deploy.ts filters with the loose form because it PRESERVES an operator's
    // existing .env keys across a redeploy; using the strict form there would
    // silently delete anything it rejects from a file we don't own.
    for (const key of ["CAURA_API_KEY2", "MEMCLAW_API_KEY2", "CAURA_lower"]) { // legacy-name-ok: rule 3 — an old-prefixed key must survive a redeploy
      assert.equal(hasPluginEnvPrefix(key), true, `${key} must survive a redeploy`);
      assert.equal(isPluginEnvKey(key), false, `${key} must not reach process.env`);
    }
  });

  test("hasPluginEnvPrefix still refuses foreign keys", () => {
    for (const key of ["PATH", "NODE_OPTIONS", "CAURAX_API_KEY", "XCAURA_API_KEY"]) {
      assert.equal(hasPluginEnvPrefix(key), false, `${key} is not ours to carry`);
    }
  });

  test("still refuses everything else", () => {
    for (const key of [
      "PATH",
      "NODE_OPTIONS",
      "LD_PRELOAD",
      "CAURA",
      "CAURAX_API_KEY",
      "XCAURA_API_KEY",
      "caura_api_key",
      "CAURA_api_key",
      "CAURA_API-KEY",
      " CAURA_API_KEY",
    ]) {
      assert.equal(isPluginEnvKey(key), false, `${key} must not be settable from .env`);
    }
  });
});

describe("readEnv call sites — alias pairs stay in step", () => {
  test("every pair is the same suffix under both prefixes", async () => {
    const { readFile } = await import("node:fs/promises");
    const { fileURLToPath } = await import("node:url");
    const { dirname, join } = await import("node:path");
    const here = dirname(fileURLToPath(import.meta.url));
    // Source-scraped for the same reason as the default pin above: the risk is a
    // typo in a literal, which no runtime test can see. A mismatched suffix
    // type-checks, passes any test that only sets the new name, and strands the
    // old one forever — the one thing rule 3 forbids.
    const { readdir } = await import("node:fs/promises");
    const srcDir = join(here, "..", "src");
    // Globbed, not listed: a new module with a dual-read call site would be
    // silently unchecked by a hardcoded list, and the floor below cannot catch
    // that because a new file only ever adds pairs.
    const files = (await readdir(srcDir)).filter(
      (f) => f.endsWith(".ts") && !f.endsWith(".test.ts"),
    );
    // Covers the wrapper helpers too — those are the multi-line call sites,
    // where a mismatched suffix is hardest to spot by eye.
    const callSite =
      /(?:readEnv|_readBoolEnv|_readIntEnv)\(\s*\[\s*"([A-Z_]+)"\s*,\s*"([A-Z_]+)"\s*,?\s*\]/gs;
    let pairs = 0;
    for (const file of files) {
      const src = await readFile(join(srcDir, file), "utf8");
      for (const [, newName, oldName] of src.matchAll(callSite)) {
        pairs++;
        assert.ok(
          newName.startsWith("CAURA_"),
          `${file}: first alias must be the new name, got ${newName}`,
        );
        assert.equal(
          oldName,
          `MEMCLAW_${newName.slice("CAURA_".length)}`, // legacy-name-ok: rule 3 — asserts the old alias tracks the new one
          `${file}: ${newName} is paired with ${oldName}, which is a different variable — the old name would never be read`,
        );
      }
    }
    assert.ok(pairs >= 28, `expected every dual-read call site to be scanned, found ${pairs}`);
  });
});

describe("readEnv — new/old env-name dual-read", () => {
  const NEW = "CAURA_TEST_DUAL_READ";
  const OLD = "MEMCLAW_TEST_DUAL_READ"; // legacy-name-ok: rule 3 — the old alias this suite pins
  const aliases = [NEW, OLD];

  afterEach(() => {
    delete process.env[NEW];
    delete process.env[OLD];
  });

  test("new name wins when both are set", () => {
    process.env[NEW] = "new-value";
    process.env[OLD] = "old-value";
    assert.equal(readEnv(aliases), "new-value");
  });

  test("old name is still read when the new one is absent", () => {
    process.env[OLD] = "old-value";
    assert.equal(readEnv(aliases), "old-value");
  });

  test("new name is read when the old one is absent", () => {
    process.env[NEW] = "new-value";
    assert.equal(readEnv(aliases), "new-value");
  });

  test("a blank new name does not shadow a working old one", () => {
    // The regression this guards: a single "first defined wins" pass would
    // return "" here and strand an install that only sets the old name.
    process.env[NEW] = "";
    process.env[OLD] = "old-value";
    assert.equal(readEnv(aliases), "old-value");
  });

  test("a blank value is honoured when it is the only alias set", () => {
    process.env[NEW] = "";
    assert.equal(readEnv(aliases), "", "KEY= must still mean 'set to empty'");
  });

  test("undefined when no alias is set", () => {
    assert.equal(readEnv(aliases), undefined);
  });
});
