/**
 * Tests for the process-level bootstrap memoization (CAURA-000).
 *
 * Pins the contract that ``MemClawContextEngine.bootstrap()`` runs the
 * smoke test (write → search → delete) AT MOST ONCE per Node process,
 * regardless of how many engine instances OpenClaw constructs. The
 * customer's goodclaw window logged 390 ``ContextEngine bootstrap``
 * events in 31.4h pre-fix (12/hr); post-fix expectation is 1 per process
 * lifetime (with retry-on-failure preserved).
 *
 * The tests mock ``globalThis.fetch`` and count "smoke-shaped" requests
 * (``POST /memories`` with ``__smoke_test__`` tag in the body) — this
 * isolates the memoization contract from the rest of the smoke test's
 * internal mechanics (parseSearchItems, similarity threshold, etc.).
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

// Set required env before importing the module so resolveTenantId
// short-circuits without hitting the network.
process.env.MEMCLAW_API_KEY = "mc_test_key_for_bootstrap_memo";
process.env.MEMCLAW_API_URL = "http://localhost:8000";
process.env.MEMCLAW_TENANT_ID = "t_test";

const { MemClawContextEngine, _resetBootstrapForTests } = await import(
  "./context-engine.js"
);

interface CapturedCall {
  url: string;
  method: string;
  body?: unknown;
}

let originalFetch: typeof fetch;
let calls: CapturedCall[];
let originalLog: typeof console.log;
let originalWarn: typeof console.warn;
let originalError: typeof console.error;

/**
 * Mock fetch that returns a memory ID for the smoke-test write, returns
 * a high-similarity hit for the smoke-test search, and 204s on the
 * cleanup DELETE. Counts requests in ``calls`` so tests can pin the
 * exact request shape.
 */
function installSmokeMockFetch(): void {
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const bodyStr = typeof init?.body === "string" ? init.body : undefined;
    let body: unknown;
    try {
      body = bodyStr ? JSON.parse(bodyStr) : undefined;
    } catch {
      body = bodyStr;
    }
    calls.push({ url, method, body });

    // Mock responses per endpoint shape:
    if (url.includes("/memories") && method === "POST") {
      // Smoke-test write → return a memory ID
      return new Response(
        JSON.stringify({ id: "00000000-0000-0000-0000-000000000001" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.includes("/search") && method === "POST") {
      // Smoke-test search → return a high-similarity hit so the smoke
      // test passes on attempt 0 (no 500ms-retry stalls).
      return new Response(
        JSON.stringify({
          items: [
            {
              id: "00000000-0000-0000-0000-000000000001",
              score: 0.95,
              content: (body as { query?: string })?.query ?? "",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.includes("/memories/") && method === "DELETE") {
      return new Response(null, { status: 204 });
    }
    return new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

function silenceConsole(): void {
  originalLog = console.log;
  originalWarn = console.warn;
  originalError = console.error;
  console.log = () => {};
  console.warn = () => {};
  console.error = () => {};
}
function restoreConsole(): void {
  console.log = originalLog;
  console.warn = originalWarn;
  console.error = originalError;
}

function countSmokeWrites(): number {
  return calls.filter(
    (c) =>
      c.method === "POST" &&
      c.url.includes("/memories") &&
      !c.url.includes("/memories/") && // exclude DELETE /memories/{id}
      (c.body as { tags?: string[] })?.tags?.includes("__smoke_test__") === true,
  ).length;
}

describe("MemClawContextEngine — process-level bootstrap memoization (CAURA-000)", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    installSmokeMockFetch();
    silenceConsole();
    _resetBootstrapForTests();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    restoreConsole();
    _resetBootstrapForTests();
  });

  test("two engine instances share one smoke test (the headline fix)", async () => {
    const engine1 = new MemClawContextEngine({ sessionId: "session-a" });
    const engine2 = new MemClawContextEngine({ sessionId: "session-b" });

    await engine1.bootstrap();
    await engine2.bootstrap();

    assert.equal(
      countSmokeWrites(),
      1,
      "expected exactly 1 smoke-test write across both engine instances " +
        "(was: 2 pre-fix — every fresh engine ran its own smoke). " +
        `Saw ${countSmokeWrites()} smoke writes in ${calls.length} total fetch calls.`,
    );
  });

  test("ten concurrent engine instances share one smoke test (race-safe)", async () => {
    // The ``if (!_x) _x = ...`` pattern is safe in Node's single-threaded
    // event loop because the first synchronous assignment wins and every
    // other concurrent caller sees the already-set promise. This test
    // pins that guarantee — a regression to per-instance state would
    // race-trigger N smoke writes here.
    const engines = Array.from(
      { length: 10 },
      (_, i) => new MemClawContextEngine({ sessionId: `session-${i}` }),
    );
    await Promise.all(engines.map((e) => e.bootstrap()));

    assert.equal(
      countSmokeWrites(),
      1,
      `expected 1 smoke write across 10 concurrent engines; saw ${countSmokeWrites()}`,
    );
  });

  test("after success, subsequent bootstrap() calls are instant no-ops", async () => {
    const engine = new MemClawContextEngine({ sessionId: "session-a" });
    await engine.bootstrap();
    const callsAfterFirst = calls.length;

    // Bootstrap again on the same engine + on fresh engines — none should
    // trigger new fetches.
    await engine.bootstrap();
    await new MemClawContextEngine({ sessionId: "session-b" }).bootstrap();
    await new MemClawContextEngine({ sessionId: "session-c" }).bootstrap();

    assert.equal(
      calls.length,
      callsAfterFirst,
      `subsequent bootstrap() calls must NOT fetch; saw ${calls.length - callsAfterFirst} extra calls`,
    );
  });

  test("backend 500 on smoke write does NOT reject bootstrap (existing contract preserved)", async () => {
    // ``_doBootstrap`` has an inner try/catch that swallows smoke-write
    // failures via ``logErrorCritical("SMOKE TEST ERROR", ...)``. This
    // was the pre-memoization behavior and we PRESERVE it: a sick
    // backend should not crash plugin lifecycle hooks. Pin this here so
    // a future "make bootstrap fail loud" refactor has to also update
    // every caller (``ingest`` / ``assemble`` / ``afterTurn`` all
    // ``await this.bootstrap()`` and would start throwing).
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });
      // EVERY smoke-related request returns 500 — write, search, delete
      if (url.includes("/memories") || url.includes("/search")) {
        return new Response("backend unavailable", { status: 500 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    const engine = new MemClawContextEngine({ sessionId: "sick-backend" });
    // MUST NOT throw — the smoke is internal-best-effort, lifecycle hooks
    // continue regardless. (Customer ground truth: their 2 SMOKE TEST
    // ERROR log lines on 06-07 13:14/13:22 did NOT prevent subsequent
    // ingest/assemble calls from succeeding once the backend came back.)
    await engine.bootstrap();
    // And the memoization holds — second engine doesn't re-fail and
    // re-log; one error per process, not per engine.
    const callsAfterFirst = calls.length;
    await new MemClawContextEngine({ sessionId: "subsequent" }).bootstrap();
    assert.equal(
      calls.length,
      callsAfterFirst,
      "subsequent bootstrap on sick backend must use memoized result " +
        `(saw ${calls.length - callsAfterFirst} extra calls)`,
    );
  });
});
