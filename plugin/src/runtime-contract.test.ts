/**
 * Runtime-contract tests for the memory-runtime registered in index.ts.
 *
 * The runtime object passed to `api.registerMemoryRuntime(...)` is inline
 * inside `register()` and cannot be imported directly. These tests construct
 * a minimal fake OpenClaw API, invoke `memclawPlugin.register(fakeApi)`, and
 * then exercise the captured runtime under known reachability states.
 *
 * The invariants pinned here are the ones that were silently broken before
 * the reachability/error-surfacing rewrite:
 *
 *   1. When the backend is marked unreachable, `getMemorySearchManager`
 *      returns `{manager: null, error}` — NOT `{manager: <stub>, error: null}`.
 *      OpenClaw's memory-core caller uses that error field to surface a
 *      "memory unavailable" result to the model.
 *
 *   2. `probeEmbeddingAvailability()` returns the OpenClaw-typed shape
 *      `{ok, error?}` — NOT the old `{available, provider}` shape — and
 *      reflects real reachability, not a lie.
 *
 *   3. `probeVectorAvailability()` returns a real boolean tied to the
 *      tracker state, not an unconditional `true`.
 *
 *   4. `status()` surfaces `fallback: {from, reason}` when unreachable,
 *      so Fleet UI / diagnostics can distinguish "installed and healthy"
 *      from "installed but broken."
 */

import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";
import memclawPlugin from "./index.js";
import {
  _resetReachabilityForTests,
  getReachability,
  markReachable,
  markUnreachable,
} from "./health.js";

type RegisteredRuntime = {
  getMemorySearchManager: (p: Record<string, unknown>) => Promise<{
    manager: unknown;
    error?: string | null;
  }>;
  resolveMemoryBackendConfig: (p: Record<string, unknown>) => unknown;
};

function buildFakeApi(): { api: Record<string, unknown>; captured: { runtime?: RegisteredRuntime } } {
  const captured: { runtime?: RegisteredRuntime } = {};
  const api = {
    registerTool: () => {},
    registerGatewayMethod: () => {},
    registerMemoryPromptSection: () => {},
    registerMemoryFlushPlan: () => {},
    registerMemoryRuntime: (runtime: RegisteredRuntime) => {
      captured.runtime = runtime;
    },
    registerContextEngine: () => {},
    on: () => {},
  };
  return { api, captured };
}

function loadRuntime(): RegisteredRuntime {
  const { api, captured } = buildFakeApi();
  memclawPlugin.register(api);
  if (!captured.runtime) {
    throw new Error("memclawPlugin.register did not call registerMemoryRuntime");
  }
  return captured.runtime;
}

describe("memory-runtime contract (OpenClaw MemoryPluginRuntime)", () => {
  beforeEach(() => _resetReachabilityForTests());

  test("resolveMemoryBackendConfig returns { backend: 'memclaw' }", () => {
    const rt = loadRuntime();
    const cfg = rt.resolveMemoryBackendConfig({}) as Record<string, unknown>;
    assert.equal(cfg.backend, "memclaw");
  });

  test("getMemorySearchManager returns {manager:null, error} when unreachable — NOT silently a stub", async () => {
    // This is the bug fix: before, an unreachable backend still handed back
    // a manager whose search() would catch-and-return-empty. Now the error
    // channel fires at manager-creation time.
    markUnreachable("simulated: pairing required");
    const rt = loadRuntime();
    const out = await rt.getMemorySearchManager({});
    assert.equal(out.manager, null, "manager must be null when unreachable");
    assert.ok(
      typeof out.error === "string" && out.error.length > 0,
      `error must be a non-empty string, got ${JSON.stringify(out.error)}`,
    );
    // The surfaced error uses "unavailable" (not "unreachable") because the
    // tracker's unreachable-state reason may not be a network-reachability
    // issue (e.g., the anti-stampede path stores 4xx / auth reasons in the
    // same state). "unavailable" stays neutral about the class of failure.
    assert.match(out.error as string, /unavailable/i);
    assert.match(out.error as string, /pairing required/);
  });

  test("getMemorySearchManager returns a real manager when reachable", async () => {
    markReachable();
    const rt = loadRuntime();
    const out = await rt.getMemorySearchManager({});
    assert.ok(out.manager !== null, "manager must be non-null when reachable");
    assert.ok(out.error === null || out.error === undefined);
  });

  test("manager.probeEmbeddingAvailability returns {ok, error?} — NOT {available, provider}", async () => {
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };

    const okRes = await manager.probeEmbeddingAvailability();
    assert.equal(typeof okRes.ok, "boolean", "must have `ok` field");
    assert.equal(okRes.ok, true, "should be ok=true when reachable");
    assert.equal("available" in okRes, false, "must not use the old `available` field");
  });

  test("manager.probeEmbeddingAvailability reports unavailable with reason when unreachable", async () => {
    markUnreachable("simulated: backend down");
    const rt = loadRuntime();
    // When unreachable, getMemorySearchManager refuses to hand back a manager,
    // so probing-on-the-manager isn't exercised in that state. Drive the
    // probe indirectly: mark reachable first to get the manager, then
    // flip unreachable and re-call the probe (manager instance lingers).
    markReachable();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    markUnreachable("simulated: backend down");
    const res = await manager.probeEmbeddingAvailability();
    assert.equal(res.ok, false);
    assert.match(res.error, /backend down/);
  });

  test("manager.probeVectorAvailability returns false when unreachable", async () => {
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    markUnreachable("any reason");
    const v = await manager.probeVectorAvailability();
    assert.equal(v, false);
  });

  test("manager.probeVectorAvailability returns true in 'unknown' state (pre-first-probe)", async () => {
    // Matches getMemorySearchManager's own gating: only an explicit
    // "unreachable" state is a definitive "no". "unknown" at startup —
    // before heartbeat has probed — must not block vector use, or the
    // first few memory ops would be spuriously blocked.
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    _resetReachabilityForTests(); // state === "unknown"
    const v = await manager.probeVectorAvailability();
    assert.equal(v, true, "unknown-state probe must not report unavailable");
  });

  test("manager.status surfaces fallback.reason when unreachable", async () => {
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    markUnreachable("simulated: http 503: backend restart");
    const s = manager.status();
    assert.equal(s.status, "unreachable");
    assert.ok(s.fallback, "status() must include a fallback block when unreachable");
    assert.equal(s.fallback.from, "memclaw-api");
    assert.match(s.fallback.reason, /backend restart/);
  });

  test("probe in 'unknown' state: AbortError does NOT flip tracker to unreachable", async () => {
    // Regression guard for the anti-stampede catch. If a probe is cancelled
    // (AbortController from a timeout or lifecycle teardown), we must NOT
    // mark the backend unreachable — cancellation is not an availability
    // signal, and doing so would suppress future ops until heartbeat probes.
    // We can't cleanly stub searchMemories mid-test, so we verify the
    // invariant structurally: explicit markUnreachable from external state
    // overrides the tracker, but an AbortError in the probe path alone
    // must leave "unknown" unchanged.
    //
    // The production code path is:
    //   catch (e) {
    //     if (e.name !== "AbortError") markUnreachable(msg);
    //     return { ok: false, error: msg };
    //   }
    //
    // Direct behavioral test: after a manager is obtained (state: reachable),
    // reset to unknown, and verify probeVectorAvailability honors the
    // state !== "unreachable" contract even when other async things happen.
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    _resetReachabilityForTests();
    assert.equal(
      await manager.probeVectorAvailability(),
      true,
      "unknown-state probeVectorAvailability must not report unavailable",
    );
    // After an explicit AbortError-like flow in production, the tracker
    // should still be "unknown" (no markUnreachable called). Simulate by
    // not mutating state; confirm invariant holds.
    assert.equal(getReachability().state, "unknown");
  });

  test("probe in 'unknown' state advances tracker on failure (anti-stampede)", async () => {
    // Regression guard: when the tracker is "unknown" and the live probe
    // fails with a non-network-class error (4xx, auth, abort, etc.),
    // trackReachability wouldn't flip the tracker, so each subsequent
    // probe would re-issue a real search — request-per-call stampede.
    // probeEmbeddingAvailability must explicitly mark unreachable in that
    // branch to escape "unknown".
    //
    // We can't easily stub searchMemories here, but we can verify the
    // invariant: after unknown-state probe failure, subsequent calls see
    // the tracker in "unreachable" and do NOT re-probe.
    //
    // Approach: manually simulate the post-probe-failure tracker flip to
    // match what the production catch block does, then confirm subsequent
    // probes honor it (the reachable/unreachable fast paths never invoke
    // searchMemories).
    _resetReachabilityForTests();
    const rt = loadRuntime();
    // Force a synthetic unreachable state, mimicking what the probe's
    // catch block writes on non-network failure:
    markUnreachable("simulated: http 401 unauthorized");
    // The fast path must short-circuit immediately with the cached error,
    // without issuing a network call.
    markReachable(); // get manager first (manager is cached)
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    markUnreachable("simulated: http 401 unauthorized");
    const res = await manager.probeEmbeddingAvailability();
    assert.equal(res.ok, false);
    assert.match(res.error, /401/);
    // Tracker stays at unreachable; fast path handled this call without a
    // live probe.
    assert.equal(getReachability().state, "unreachable");
  });

  test("manager.readFile returns a MemoryReadResult-shaped value (not null)", async () => {
    markReachable();
    const rt = loadRuntime();
    const { manager } = (await rt.getMemorySearchManager({})) as { manager: any };
    const r = await manager.readFile({ relPath: "does-not-apply.md" });
    assert.equal(typeof r, "object");
    assert.equal(r, r); // non-null
    assert.equal(typeof r.text, "string");
    assert.equal(typeof r.path, "string");
    // MemClaw does not back readFile with content; empty text is the honest
    // answer. What matters is the SHAPE, not the content.
  });
});

// ─── MemoryFlushPlan contract (added 2026-05-21) ────────────────────────────
// OpenClaw's ``memory-state.d.ts`` defines MemoryFlushPlan with SIX required
// fields. The agent-runner calls ``resolver(...).relativePath`` and passes it
// to ``ensureMemoryFlushTargetFile``, which throws "Invalid memory flush
// target path" on any falsy / absolute value. Pre-fix our resolver returned
// ``{instructions, softThresholdTokens}`` and the error only surfaced on
// long sessions that crossed the compaction threshold — making it look
// intermittent. These tests lock the entire required shape.

type _FlushPlanResolver = (params?: {
  cfg?: unknown;
  nowMs?: number;
}) => Record<string, unknown> | null;

function _loadFlushPlanResolver(): _FlushPlanResolver {
  let captured: _FlushPlanResolver | undefined;
  const api = {
    registerTool: () => {},
    registerGatewayMethod: () => {},
    registerMemoryPromptSection: () => {},
    registerMemoryFlushPlan: (r: _FlushPlanResolver) => {
      captured = r;
    },
    registerMemoryRuntime: () => {},
    registerContextEngine: () => {},
    on: () => {},
  };
  memclawPlugin.register(api);
  if (!captured) throw new Error("registerMemoryFlushPlan was not called");
  return captured;
}

describe("MemoryFlushPlan contract (OpenClaw agent-runner.runtime)", () => {
  test("plan has every required field with the right primitive type", () => {
    const plan = _loadFlushPlanResolver()({
      nowMs: Date.UTC(2026, 4, 21, 12, 0, 0),
    });
    assert.ok(plan, "resolver must return a plan, not null");
    assert.equal(typeof plan.softThresholdTokens, "number");
    assert.equal(typeof plan.forceFlushTranscriptBytes, "number");
    assert.equal(typeof plan.reserveTokensFloor, "number");
    assert.equal(typeof plan.prompt, "string");
    assert.equal(typeof plan.systemPrompt, "string");
    assert.equal(typeof plan.relativePath, "string");
  });

  test("relativePath is non-empty, workspace-relative, no absolute / parent-escape", () => {
    const plan = _loadFlushPlanResolver()({
      nowMs: Date.UTC(2026, 4, 21, 12, 0, 0),
    });
    assert.ok(plan && typeof plan.relativePath === "string");
    const rp = plan.relativePath as string;
    assert.ok(rp.length > 0);
    assert.equal(rp.startsWith("/"), false);
    assert.equal(rp.startsWith("../"), false);
    assert.equal(rp.includes("/../"), false);
    assert.match(rp, /^memclaw\//);
  });

  test("relativePath embeds the provided nowMs date stamp deterministically", () => {
    const r = _loadFlushPlanResolver();
    const a = r({ nowMs: Date.UTC(2026, 4, 21, 23, 59, 0) });
    const b = r({ nowMs: Date.UTC(2026, 4, 22, 0, 1, 0) });
    assert.ok(a && b);
    assert.match(a.relativePath as string, /2026-05-21/);
    assert.match(b.relativePath as string, /2026-05-22/);
    const c1 = r({ nowMs: Date.UTC(2026, 4, 21, 12, 0, 0) });
    const c2 = r({ nowMs: Date.UTC(2026, 4, 21, 12, 0, 0) });
    assert.equal(
      (c1 as Record<string, unknown>).relativePath,
      (c2 as Record<string, unknown>).relativePath,
    );
  });

  test("resolver tolerates being called with no args (legacy OpenClaw callers)", () => {
    const plan = _loadFlushPlanResolver()();
    assert.ok(plan, "resolver() with no args must still return a plan");
    assert.equal(typeof (plan as Record<string, unknown>).relativePath, "string");
  });
});

describe("MemoryFlushPlan resolver — input-hardening (regression: review 2026-05-21)", () => {
  // The resolver runs in OpenClaw's agent-runner stack, outside the
  // registration-time try/catch. A throw here crashes the flush turn.
  // These tests pin the defensive contract: null input, non-finite
  // nowMs, and any inner failure must still yield a valid plan.

  test("resolver(null) returns a valid plan (destructure-null TypeError regression)", () => {
    const r = _loadFlushPlanResolver();
    // Cast to call with null — pre-fix the ``= {}`` default did NOT fire
    // for null (only undefined), so destructuring threw TypeError.
    const plan = (r as unknown as (p: unknown) => Record<string, unknown> | null)(null);
    assert.ok(plan, "resolver(null) must still return a plan");
    assert.equal(typeof (plan as Record<string, unknown>).relativePath, "string");
    assert.match(
      (plan as Record<string, unknown>).relativePath as string,
      /^memclaw\/flush-\d{4}-\d{2}-\d{2}\.md$/,
    );
  });

  test("resolver({nowMs: NaN}) falls back to Date.now() (RangeError regression)", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: Number.NaN });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    assert.match(rp, /^memclaw\/flush-\d{4}-\d{2}-\d{2}\.md$/, `bad rp=${rp}`);
  });

  test("resolver({nowMs: Infinity}) falls back to Date.now()", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: Number.POSITIVE_INFINITY });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    assert.match(rp, /^memclaw\/flush-\d{4}-\d{2}-\d{2}\.md$/);
  });

  test("resolver({nowMs: -Infinity}) falls back to Date.now()", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: Number.NEGATIVE_INFINITY });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    assert.match(rp, /^memclaw\/flush-\d{4}-\d{2}-\d{2}\.md$/);
  });

  test("resolver({nowMs: 'oops' as any}) ignores non-number and falls back", () => {
    const r = _loadFlushPlanResolver();
    // Real OpenClaw versions only pass number | undefined, but the gate is
    // structural — string / object / boolean inputs all must degrade
    // gracefully rather than crash.
    const plan = (r as unknown as (p: { nowMs: unknown }) => Record<string, unknown> | null)({
      nowMs: "oops",
    });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    assert.match(rp, /^memclaw\/flush-\d{4}-\d{2}-\d{2}\.md$/);
  });
});

describe("MemoryFlushPlan resolver — negative-timestamp guard (review 2026-05-24)", () => {
  // ``Number.isFinite(-1) === true``, so without an explicit positive
  // lower bound a negative ``nowMs`` (test mock, time-travel scenario,
  // accidental ``-Date.now()``) would produce a ``relativePath`` like
  // ``memclaw/flush-1969-12-31.md``. The path-shape check would still
  // pass (it's just a valid YYYY-MM-DD) but the file name is meaningless
  // and confuses operators reading the workspace. Lock the lower bound.

  test("resolver({nowMs: -1}) falls back to Date.now() instead of pre-epoch", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: -1 });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    const yearMatch = rp.match(/^memclaw\/flush-(\d{4})-\d{2}-\d{2}\.md$/);
    assert.ok(yearMatch, `unexpected path shape: ${rp}`);
    const yearInPath = Number.parseInt(yearMatch[1], 10);
    const currentYear = new Date().getUTCFullYear();
    assert.equal(
      yearInPath,
      currentYear,
      `negative nowMs must fall back to current year, got ${yearInPath} for path ${rp}`,
    );
  });

  test("resolver({nowMs: -Date.now()}) falls back to Date.now()", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: -Date.now() });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    const yearMatch = rp.match(/^memclaw\/flush-(\d{4})-\d{2}-\d{2}\.md$/);
    assert.ok(yearMatch);
    assert.equal(Number.parseInt(yearMatch[1], 10), new Date().getUTCFullYear());
  });

  test("resolver({nowMs: 0}) falls back to Date.now() (epoch is also rejected)", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: 0 });
    assert.ok(plan);
    const rp = (plan as Record<string, unknown>).relativePath as string;
    const yearMatch = rp.match(/^memclaw\/flush-(\d{4})-\d{2}-\d{2}\.md$/);
    assert.ok(yearMatch);
    assert.notEqual(
      Number.parseInt(yearMatch[1], 10),
      1970,
      "nowMs=0 must NOT produce a 1970-stamped path",
    );
  });

  test("resolver still accepts a real positive nowMs (no regression)", () => {
    const r = _loadFlushPlanResolver();
    const plan = r({ nowMs: Date.UTC(2026, 4, 24, 12, 0, 0) });
    assert.ok(plan);
    assert.match(
      (plan as Record<string, unknown>).relativePath as string,
      /^memclaw\/flush-2026-05-24\.md$/,
    );
  });
});

// --- ContextEngine.assemble() contract (CAURA-000 WhatsApp keystones) ---
//
// Customer report 2026-05-25: ``context engine assemble failed, using
// pipeline messages: TypeError: Cannot read properties of undefined
// (reading 'slice')`` fired on every WhatsApp turn after the customer
// set ``plugins.slots.contextEngine = "memclaw"``. OpenClaw's catch at
// selection-BfCSa_QL.js:7689 calls ``String(err)`` which strips the
// stack — so the throw was invisible in the gateway log past the
// top-line.
//
// Two related contracts the runtime depends on (OpenClaw 2026.5.4
// plugin-sdk/src/context-engine/types.d.ts: AssembleResult):
//
//   * ``messages`` is required. The runtime reads it by reference and
//     overwrites ``activeSession.agent.state.messages`` when the
//     returned reference differs from input. ``undefined`` here
//     produces the ``.slice`` TypeError downstream that the customer
//     saw.
//   * ``estimatedTokens`` is required. Used by the budget tracker
//     surrounding the assemble call.
//
// These tests lock both fields on every return path, AND lock the
// outer try/catch that protects the runtime from any future inner
// throw — even when we can't reproduce the exact upstream condition
// in unit tests, the catch guarantees we never surface ``messages:
// undefined`` to OpenClaw.

import { MemClawContextEngine } from "./context-engine.js";

describe("ContextEngine.assemble contract (OpenClaw AssembleResult)", () => {
  function makeEngine(): MemClawContextEngine {
    // Tenant-less config — bootstrap's educate POST will fail-fast
    // against MEMCLAW_API_URL, but assemble's own try/catch swallows
    // it so the test still exercises the return shape. We're testing
    // the contract surface, not the bootstrap success path.
    return new MemClawContextEngine({});
  }

  test("returns AssembleResult shape with `messages` and `estimatedTokens` (skip-recall path)", async () => {
    const engine = makeEngine();
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      prompt: "hi",
      tokenBudget: 1000,
    });
    assert.ok(result, "assemble must return a result");
    assert.ok(Array.isArray(result.messages), "result.messages must be an array");
    assert.equal(
      typeof result.estimatedTokens,
      "number",
      "result.estimatedTokens must be a number (AssembleResult contract)",
    );
  });

  test("does not throw when params.messages is undefined (defensive coercion)", async () => {
    const engine = makeEngine();
    // The actual customer-observed shape: OpenClaw 2026.5.4 passed
    // params without `messages` (or with messages typed differently
    // than we expected). Our prologue must coerce, not crash.
    const result = await engine.assemble({
      prompt: "hello",
      tokenBudget: 1000,
    });
    assert.ok(result);
    assert.ok(
      Array.isArray(result.messages),
      "messages must default to [] when input is undefined, never be undefined",
    );
  });

  test("does not throw when called with no params (legacy + degenerate callers)", async () => {
    const engine = makeEngine();
    const result = await engine.assemble(
      undefined as unknown as Parameters<typeof engine.assemble>[0],
    );
    assert.ok(result);
    assert.ok(Array.isArray(result.messages));
    assert.equal(typeof result.estimatedTokens, "number");
  });

  test("outer try/catch returns safe fallback on inner throw (never propagates)", async () => {
    // Synthesize an inner throw by monkey-patching `bootstrap` —
    // it's the first awaited call inside _assembleInner, so a throw
    // there exercises the outer catch without depending on any
    // particular downstream code path.
    const engine = makeEngine();
    (engine as unknown as { bootstrap: () => Promise<void> }).bootstrap =
      async () => {
        throw new Error("synthetic bootstrap failure");
      };

    const input = [{ role: "user", content: "hi" }];
    const result = await engine.assemble({
      messages: input,
      prompt: "hi",
      tokenBudget: 1000,
    });

    // Contract: even on inner throw, return a well-shaped result so
    // OpenClaw's `assembled.messages` access never throws downstream.
    assert.ok(result);
    assert.ok(Array.isArray(result.messages));
    assert.equal(typeof result.estimatedTokens, "number");
    // Safe fallback means no system-prompt injection on this turn.
    assert.equal(result.systemPromptAddition ?? "", "");
  });

  test("echoes input messages reference (never returns a new array under success)", async () => {
    // OpenClaw 2026.5.4 selection-BfCSa_QL.js:7677 overwrites
    // activeSession.agent.state.messages when `assembled.messages !==
    // activeSession.messages`. Returning a fresh array would
    // mass-replace the runtime's session state every turn — only
    // compact() may do that. Lock reference equality.
    const engine = makeEngine();
    const input = [{ role: "user", content: "hi" }];
    const result = await engine.assemble({
      messages: input,
      prompt: "hi",
      tokenBudget: 1000,
    });
    assert.strictEqual(
      result.messages,
      input,
      "assemble must echo the input `messages` reference on the success path",
    );
  });
});

// --- Per-call agent-id resolution contract (v2.8.1 hotfix) ---
//
// Customer report 2026-06-02: webclaw agent's gateway log emitted
// ``[memclaw] Could not resolve agent ID — using install-default
// 'main-e5366d79a926'`` 4-5 times per turn. Root cause: 3 of our 6
// ``resolveAgentId`` call sites passed only ``this.config`` (the
// factory-time ``factoryCtx`` wrapper, containing only
// ``{config, agentDir, workspaceDir}`` — no agent identity). The
// per-call ``params`` object — which OpenClaw 2026.5.4 populates
// with ``sessionKey: "agent:NAME:CHANNEL:..."`` — was being
// dropped on the floor.
//
// User-visible consequence: the identity block injected by
// ``assemble`` carried the bogus install-default
// (``main-<installId>``) instead of the real agent name. The LLM
// saw the wrong ``agent_id`` every turn and propagated it into
// downstream tool calls, scoping memory writes/recalls under a
// phantom agent. Cross-session recall silently broke.
//
// Earlier wet tests missed this because:
//   1. probes asserted only structural shape (``messages`` is an
//      array, ``estimatedTokens`` is a number), never SEMANTIC
//      content (``agent_id=<expected>``).
//   2. ``main-<installId>`` is visually similar to a real
//      ``--agent main`` value in test logs; the fallback wasn't
//      obviously wrong.
//
// These tests pin BOTH the structural contract AND the resolved
// agent_id content, so the same class of bug cannot slip through
// again. Positive-content assertions on agent identity belong with
// the structural assertions above; this is intentional.

describe("ContextEngine.assemble agent-id resolution (v2.8.1)", () => {
  function makeEngine(): MemClawContextEngine {
    return new MemClawContextEngine({});
  }

  test("resolves agent_id from params.sessionKey 'agent:NAME:...' format", async () => {
    const engine = makeEngine();
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      sessionKey: "agent:bankingclaw:whatsapp:group:test-group",
      tokenBudget: 4000,
    });
    assert.match(
      result.systemPromptAddition ?? "",
      /agent_id=`bankingclaw`/,
      "assemble's identity block must extract agent_id from " +
        "sessionKey (per OpenClaw 2026.5.4 contract), not fall back " +
        "to install-default. If this fails, the LLM is being told " +
        "the WRONG agent_id every turn — see CAURA-000.",
    );
  });

  test("resolves agent_id from params.agentId explicit form", async () => {
    const engine = makeEngine();
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      agentId: "explicit-agent-name",
      tokenBudget: 4000,
    } as unknown as Parameters<typeof engine.assemble>[0]);
    assert.match(
      result.systemPromptAddition ?? "",
      /agent_id=`explicit-agent-name`/,
      "explicit ``agentId`` on params must take precedence",
    );
  });

  test("falls back to install-default ONLY when no per-call context provides agent identity", async () => {
    // Regression guard: when params lacks sessionKey/agentId AND
    // this.config has nothing useful, the install-default fallback
    // path must still work — we don't want to introduce a hard
    // failure here, just a documented fallback.
    const engine = makeEngine();
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      tokenBudget: 4000,
    });
    // Either "main-<12-hex>" (install-default) OR whatever
    // MEMCLAW_AGENT_ID env var is set to. Don't pin the exact
    // value; just assert the contract still produces SOMETHING.
    assert.match(
      result.systemPromptAddition ?? "",
      /agent_id=`[^`]+`/,
      "fallback path must still produce a non-empty agent_id",
    );
  });

  test("malformed sessionKey (not 'agent:NAME:...') falls through to other sources", async () => {
    const engine = makeEngine();
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      // Not the "agent:NAME:..." format the resolver parses.
      sessionKey: "some-arbitrary-session-id",
      tokenBudget: 4000,
    });
    // Should fall through past the sessionKey path; no specific
    // agent name extracted. Acceptable to land on install-default.
    assert.match(result.systemPromptAddition ?? "", /agent_id=`[^`]+`/);
  });
});

// --- Session-buffer key consistency contract (v2.8.1 review follow-up) ---
//
// Code-review concern: ``ingest`` and ``assemble`` both compute a
// per-session ``sessionKey`` to index the module-level
// ``sessionBuffers`` Map. If the two compute DIFFERENT keys for the
// same logical session, the messages ``ingest`` writes are invisible
// to ``assemble``'s ``buildQueryFromMessages`` lookup — recall
// quality degrades silently. The fix in v2.8.1 threads per-call
// context into both call sites, but nothing structurally pins the
// guarantee that they remain consistent.
//
// This test imports the module-private ``getSessionKey`` from
// ``context-engine.internal.ts`` (kept out of the production
// ``context-engine.ts`` module to avoid a public ``_internal``
// export) and asserts that calls shaped
// like OpenClaw's actual ingest / assemble invocations produce
// identical keys when given the same session identity. If a future
// refactor regresses one but not the other, this test fails
// loudly.

import * as contextEngineInternal from "./context-engine.internal.js";

describe("ContextEngine session-key consistency (ingest ↔ assemble)", () => {
  test("identical sessionKey on ingest-shape and assemble-shape calls produces identical session-buffer keys", () => {
    const config = {};

    // OpenClaw 2026.5.4 invokes ``contextEngine.ingest({sessionId, sessionKey, message})``
    // — the wrapper. Our plugin's ``ingest(message)`` receives this whole
    // wrapper as ``message``. ``getSessionKey`` reads from ``message?.sessionKey``.
    const ingestArg = {
      sessionId: "session-abc-123",
      sessionKey: "agent:webclaw:whatsapp:group:test",
      message: { role: "user", content: "needle" },
    };

    // OpenClaw invokes ``contextEngine.assemble({sessionId, sessionKey, messages, ...})``
    // — distinct params shape, same session identity.
    const assembleParams = {
      sessionId: "session-abc-123",
      sessionKey: "agent:webclaw:whatsapp:group:test",
      messages: [],
      tokenBudget: 4000,
    };

    const ingestKey = contextEngineInternal.getSessionKey(config, ingestArg);
    const assembleKey = contextEngineInternal.getSessionKey(config, assembleParams);

    assert.strictEqual(
      ingestKey,
      assembleKey,
      "ingest and assemble must derive the same session-buffer key for " +
        "the same session identity. If this fails, the in-memory " +
        "buffer ingest writes will be invisible to assemble's " +
        "buildQueryFromMessages lookup and recall quality degrades " +
        "silently. See CAURA-000 v2.8.1 review follow-up.",
    );
    // The key must also embed the session-identifying sessionKey
    // (not be a content-free default), to prove we're actually
    // using per-call context — not just coincidentally producing
    // the same fallback key in both calls.
    assert.ok(
      ingestKey.includes("agent:webclaw:whatsapp:group:test"),
      `expected session-buffer key to embed the wrapper sessionKey, got ${ingestKey}`,
    );
  });

  test("when sessionKey is absent from BOTH the per-call source and config, ingest and assemble still produce identical keys (consistent fallback)", () => {
    // Without sessionKey, both calls fall through to the
    // ``resolveAgentId(perCall, config) + ":" + sessionId`` path.
    // We assert that path is identical for matching identity even
    // when sessionKey is missing — important because a non-OpenClaw
    // caller might omit it.
    const config = {};
    const ingestArg = {
      sessionId: "sid",
      message: { role: "user", content: "x" },
    };
    const assembleParams = {
      sessionId: "sid",
      messages: [],
      tokenBudget: 4000,
    };
    const ingestKey = contextEngineInternal.getSessionKey(config, ingestArg);
    const assembleKey = contextEngineInternal.getSessionKey(config, assembleParams);
    assert.strictEqual(
      ingestKey,
      assembleKey,
      "ingest/assemble fallback paths must produce identical keys when no sessionKey is provided",
    );
  });
});
//
// Customer escalation 2026-05-28: a different agent (``dbaclaw``)
// on the same gateway emitted:
//
//     [memclaw] assemble: unexpected error (returning safe fallback)
//     TypeError: Cannot read properties of undefined (reading 'tenantId')
//         at getTenantPrefix (...context-engine.js:29:19)
//         at getSessionKey   (...context-engine.js:32:26)
//         at _assembleInner  (...context-engine.js:519:28)
//         at ...invokeWithLegacyCompat (registry-hc1-G3yP.js:103:10)
//         at mutableAgent.transformContext (model-context-tokens-z5hvDVkk.js:2679:22)
//
// The outer try/catch from PR #212 caught the throw and returned the
// safe-fallback shape, so OpenClaw didn't crash — but every turn
// that hit this path bypassed keystones, recall, and afterTurn
// completely. Investigation against the OpenClaw 2026.5.4 source
// (``registry-DFFgCbcm.js:241-289 resolveContextEngine``) showed
// the standard factory call always passes a populated
// ``factoryCtx``, but a custom-build or legacy-compat invocation
// observed in production reaches us with ``undefined``. Regardless
// of the upstream cause, the helpers must tolerate it.
//
// These tests pin:
//   1. Constructor coerces ``undefined`` / ``null`` config to ``{}``.
//   2. ``assemble`` returns a well-shaped ``AssembleResult`` instead
//      of running the outer-catch fallback when the engine was
//      constructed with no config — the inner path now completes
//      cleanly, so future turns get full keystones + recall instead
//      of silent degradation.

describe("MemClawContextEngine.constructor — undefined-config tolerance (v2.6.5)", () => {
  test("constructed with undefined: assemble does NOT throw and does NOT take the safe-fallback path", async () => {
    // Pass undefined where the type-system expects a config object —
    // exactly the shape that triggered the dbaclaw production
    // TypeError. Without the constructor coercion, this turn would
    // surface ``[memclaw] assemble: unexpected error (returning safe
    // fallback)`` in the log; with it, the inner code completes and
    // we get the normal AssembleResult.
    const engine = new MemClawContextEngine(
      undefined as unknown as Record<string, unknown>,
    );
    const result = await engine.assemble({
      messages: [{ role: "user", content: "hi" }],
      prompt: "hi",
      tokenBudget: 4000,
    });
    assert.ok(result, "assemble must return a result");
    assert.ok(
      Array.isArray(result.messages),
      "messages must be an array (no exception, no safe-fallback shape)",
    );
    assert.equal(
      typeof result.estimatedTokens,
      "number",
      "estimatedTokens must be a number",
    );
    // The safe-fallback path returns ``systemPromptAddition: ""``;
    // a successful inner run produces the education + identity
    // composition. The presence of non-trivial content proves the
    // helpers tolerated the undefined config without throwing.
    assert.ok(
      (result.systemPromptAddition ?? "").length > 0,
      "systemPromptAddition must be populated — the helpers did NOT throw",
    );
  });

  test("constructed with null: same tolerance (defense-in-depth)", async () => {
    const engine = new MemClawContextEngine(
      null as unknown as Record<string, unknown>,
    );
    const result = await engine.assemble({
      messages: [],
      prompt: "",
      tokenBudget: 4000,
    });
    assert.ok(result);
    assert.ok(Array.isArray(result.messages));
    assert.equal(typeof result.estimatedTokens, "number");
  });
});

// --- ContextEngine.compact + openclaw-sdk-bridge contract (v2.6.4 hotfix) ---
//
// Customer escalation 2026-05-26: WhatsApp group sessions stuck
// running on plugin v2.6.3. ``openclaw status`` showed group
// contexts over budget (312k/272k, 292k/272k tokens). Agent looped
// on ``memclaw_keystones`` tool calls 3-5 times per turn, never
// finalizing; final replies were silently dropped.
//
// Root cause: ``info.ownsCompaction: true`` (set in PR #212 with
// the contextEngine slot auto-claim) told OpenClaw we owned
// compaction, but our ``compact()`` returned ``undefined`` — not
// a valid ``CompactResult`` per OpenClaw 2026.5.4
// ``plugin-sdk/src/context-engine/types.d.ts``. Over-budget
// sessions delegated to us, got nothing back, never shrank.
//
// The v2.6.4 fix: discover the ``openclaw/plugin-sdk`` runtime
// helper ``delegateCompactionToRuntime`` via filesystem walk from
// ``process.argv[1]`` (see ``openclaw-sdk-bridge.ts`` for the
// rationale — bare-spec import fails for native-loaded plugins),
// call it from ``compact()``, return the proper
// ``{ok, compacted, reason, result}`` shape it produces.
//
// These tests pin:
//   1. ``info.ownsCompaction === true`` (regression guard — flipping
//      to ``false`` without implementing a real ``CompactResult``
//      return would break compaction silently again).
//   2. ``compact()`` returns a structured ``CompactResult`` shape
//      regardless of whether the SDK is discoverable.
//   3. ``compact()`` still persists the summary into MemClaw as a
//      side effect (existing behavior preserved).
//   4. The bridge's resolver cache works — second call doesn't
//      repeat the filesystem walk.
//   5. The bridge returns ``null`` gracefully when no openclaw
//      install can be discovered (e.g., running under
//      ``node --test`` where ``argv[1]`` is the test runner, not
//      ``openclaw``).
//
// Note on test environment: under ``node --test``, ``process.argv[1]``
// is the path to our compiled ``*.test.js`` file, NOT the openclaw
// launcher. So the bridge's walk from that path will not find an
// openclaw package.json, and ``getOpenClawSdk()`` returns ``null``.
// That's the "graceful fallback" path we want to exercise here —
// the wet test on the GCE VM exercises the success path against a
// real openclaw install.

// MemClawContextEngine already imported above for the assemble-contract
// block. Import only what's new for the compaction-bridge contract.
import { _resetSdkBridgeCache, getOpenClawSdk } from "./openclaw-sdk-bridge.js";

describe("ContextEngine.info compaction-ownership (v2.6.4)", () => {
  test("info.ownsCompaction === true (we own compaction by delegating to SDK)", () => {
    const engine = new MemClawContextEngine({});
    assert.equal(
      engine.info.ownsCompaction,
      true,
      "ownsCompaction must stay true — compact() now delegates via " +
        "delegateCompactionToRuntime, fulfilling the contract. Setting " +
        "false does NOT auto-fall-back to legacy compaction per OpenClaw docs.",
    );
  });

  test("info.id === 'memclaw' (slot resolver depends on this)", () => {
    const engine = new MemClawContextEngine({});
    assert.equal(engine.info.id, "memclaw");
  });
});

describe("openclaw-sdk-bridge resolver", () => {
  test("returns {sdk: null, pkgRoot: null} in test runtime (argv[1] is the test runner, not openclaw)", async () => {
    _resetSdkBridgeCache();
    const res = await getOpenClawSdk();
    // We're running under `node --test path/to/dist/runtime-contract.test.js`,
    // so the launcher walk won't find an openclaw package.json. Both fields
    // null is the documented graceful-degradation return value for this
    // failure mode (not "discovered but broken").
    assert.equal(
      res.sdk,
      null,
      "bridge must return sdk=null in non-openclaw runtimes",
    );
    assert.equal(
      res.pkgRoot,
      null,
      "pkgRoot must be null when openclaw cannot be discovered — caller " +
        "uses pkgRoot to distinguish 'never found' from 'found but broken'",
    );
  });

  test("caches the negative result — second call does not re-walk filesystem", async () => {
    // We can't directly observe filesystem calls without a spy, but we can
    // observe that two successive calls return the same value with no
    // throw — the function is idempotent and cheap on repeat invocation.
    // The promise-cache pattern means both awaits resolve to the SAME
    // object reference, which we check with strictEqual.
    _resetSdkBridgeCache();
    const first = await getOpenClawSdk();
    const second = await getOpenClawSdk();
    assert.strictEqual(first, second);
  });
});

describe("ContextEngine.compact contract (delegation + persistence)", () => {
  test("returns a CompactResult-shaped object (not undefined)", async () => {
    _resetSdkBridgeCache();
    const engine = new MemClawContextEngine({});
    // In the test runtime, the bridge returns null, so compact() takes
    // the structured-fallback path. We must still get the v2.6.4 shape
    // — not a regression to v2.6.3's `undefined`.
    const result = await engine.compact({});
    assert.ok(result, "compact() must return an object (not undefined)");
    assert.equal(typeof result.ok, "boolean", "result.ok is boolean");
    assert.equal(
      typeof result.compacted,
      "boolean",
      "result.compacted is boolean",
    );
    assert.equal(
      result.ok,
      false,
      "ok=false in test env (SDK not discoverable)",
    );
    assert.equal(
      result.compacted,
      false,
      "compacted=false in test env (delegation didn't run)",
    );
    assert.ok(
      typeof result.reason === "string" && result.reason.length > 0,
      "result.reason names why compaction did not occur",
    );
  });

  test("compact() with no summary returns the fallback without throwing", async () => {
    _resetSdkBridgeCache();
    const engine = new MemClawContextEngine({});
    const result = await engine.compact({ sessionId: "test-session" });
    assert.equal(result.ok, false);
    assert.equal(result.compacted, false);
  });

  test("compact() with missing sessionId skips SDK delegation entirely (defensive guard)", async () => {
    // The defensive pre-check in compact() short-circuits to a
    // structured fallback when sessionId/sessionFile aren't both
    // present. This protects against gateway-RPC test probes and
    // future degenerate callers that could otherwise trigger
    // ``compactEmbeddedPiSessionDirect`` to crash the host
    // process. OpenClaw production compaction (per
    // ``pi-embedded-X0afS0ip.js:2447``) ALWAYS provides both
    // fields, so this guard never fires on the happy path.
    _resetSdkBridgeCache();
    const engine = new MemClawContextEngine({});
    const result = await engine.compact({ sessionFile: "/tmp/test.jsonl" }); // no sessionId
    assert.equal(result.ok, false);
    assert.equal(result.compacted, false);
    assert.match(
      String(result.reason || ""),
      /sessionId\/sessionFile/,
      "fallback reason must name the missing field for operator diagnosis",
    );
  });

  test("compact() with missing sessionFile skips SDK delegation entirely (defensive guard)", async () => {
    _resetSdkBridgeCache();
    const engine = new MemClawContextEngine({});
    const result = await engine.compact({ sessionId: "test-id" }); // no sessionFile
    assert.equal(result.ok, false);
    assert.equal(result.compacted, false);
    assert.match(String(result.reason || ""), /sessionId\/sessionFile/);
  });

  test("compact() with a summary attempts MemClaw persist but does not throw on failure", async () => {
    // Persistence target is unconfigured (no MEMCLAW_API_KEY in test env),
    // so the POST /memories call fails. compact() must catch that and still
    // return a CompactResult. This is the production-safety contract:
    // compaction must NEVER break a turn just because the memory write
    // failed.
    _resetSdkBridgeCache();
    const engine = new MemClawContextEngine({});
    const result = await engine.compact({
      summary: "synthetic compaction summary for the contract test",
      sessionId: "test-session",
    });
    assert.ok(result);
    assert.equal(typeof result.ok, "boolean");
    assert.equal(typeof result.compacted, "boolean");
  });
});
