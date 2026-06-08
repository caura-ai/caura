/**
 * Tests for ``resolveAgentId`` / ``resolveAgentIdQuiet``.
 *
 * The quiet variant exists for ``ContextEngine`` bootstrap, where the
 * factoryCtx wrapper that OpenClaw passes legitimately carries no per-call
 * session identity — falling back to ``main-${installId}`` IS the design.
 * The loud variant guards per-turn paths (assemble/ingest/afterTurn/
 * prepareSubagentSpawn) where a fall-through is a real bug we want to see.
 *
 * Customer-side ground truth motivating this split: 18h goodclaw window
 * post-2.8.1 shows exactly 1.00 ``Could not resolve agent ID`` warns per
 * ``ContextEngine bootstrap`` event — i.e. 100% of residual warn noise
 * comes from the bootstrap fallback. This test pins the contract so the
 * fix doesn't accidentally also silence the per-turn diagnostic.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

// Clear any inherited test pollution so the fallback path actually runs.
delete process.env.MEMCLAW_AGENT_ID;

const { resolveAgentId, resolveAgentIdQuiet } = await import(
  "./resolve-agent.js"
);

let originalWarn: typeof console.warn;
let warnLines: string[];

function captureWarn(): void {
  warnLines = [];
  originalWarn = console.warn;
  console.warn = (...args: unknown[]) => {
    warnLines.push(args.map((a) => String(a)).join(" "));
  };
}
function restoreWarn(): void {
  console.warn = originalWarn;
}

describe("resolveAgentId / resolveAgentIdQuiet — fallback warn semantics", () => {
  beforeEach(() => captureWarn());
  afterEach(() => restoreWarn());

  test("loud variant emits warn when falling through to install-default", () => {
    const id = resolveAgentId({}); // empty source → falls through
    assert.match(id, /^main-[0-9a-f]+$/);
    assert.equal(warnLines.length, 1, `expected 1 warn; got ${warnLines.length}: ${warnLines.join(" | ")}`);
    assert.match(warnLines[0], /Could not resolve agent ID/);
    assert.match(warnLines[0], /install-default/);
  });

  test("quiet variant returns same fallback but emits NO warn", () => {
    const id = resolveAgentIdQuiet({});
    assert.match(id, /^main-[0-9a-f]+$/);
    assert.equal(warnLines.length, 0, `expected 0 warns from quiet path; got: ${warnLines.join(" | ")}`);
  });

  test("loud variant does NOT warn when the source resolves (no spurious noise)", () => {
    const id = resolveAgentId({ sessionKey: "agent:goodclaw:whatsapp:direct:+972544576576" });
    assert.equal(id, "goodclaw");
    assert.equal(warnLines.length, 0, `expected 0 warns on successful resolve; got: ${warnLines.join(" | ")}`);
  });

  test("quiet variant ALSO resolves correctly when source is good (only the warn is silenced)", () => {
    const id = resolveAgentIdQuiet({ sessionKey: "agent:dbaclaw:slack:#general" });
    assert.equal(id, "dbaclaw");
    assert.equal(warnLines.length, 0);
  });

  test("multiple sources: first match wins regardless of variant (pin precedence)", () => {
    // Explicit agentId in perCall must win over sessionKey in config.
    const id = resolveAgentId(
      { agentId: "explicit-from-percall" },
      { sessionKey: "agent:from-config:main" },
    );
    assert.equal(id, "explicit-from-percall");
    assert.equal(warnLines.length, 0);
  });
});
