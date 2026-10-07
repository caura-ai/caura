/**
 * Isolated runtime regression fixture, launched by context-engine.test.ts. The
 * pre-compaction memory-flush turn asks the agent to caura_write a session
 * summary, so CAURA_AUTO_WRITE_TURNS covers it too (M-106).
 */
import assert from "node:assert/strict";

type FlushPlanResolver = (params?: { nowMs?: number }) => Record<string, unknown> | null;

const enabled = process.argv[2] === "enabled";
// register() starts its boot probes; none of them may reach a network.
globalThis.fetch = async () => {
  throw new Error("no network in this fixture");
};
const { default: cauraPlugin } = await import("./index.js");

let resolveFlushPlan: FlushPlanResolver | undefined;
cauraPlugin.register({
  registerTool: () => {},
  registerGatewayMethod: () => {},
  registerMemoryPromptSection: () => {},
  registerMemoryFlushPlan: (resolver: FlushPlanResolver) => {
    resolveFlushPlan = resolver;
  },
  registerMemoryRuntime: () => {},
  registerContextEngine: () => {},
  on: () => {},
});
if (!resolveFlushPlan) throw new Error("registerMemoryFlushPlan was not called");

const plan = resolveFlushPlan({ nowMs: Date.UTC(2026, 9, 5, 12) });
if (enabled) {
  assert.ok(plan, "the flush turn runs while automatic writes are on");
  assert.match(String(plan.prompt), /caura_write/);
} else {
  // OpenClaw runs no memory-flush turn when the resolver returns null.
  assert.equal(plan, null, "no flush turn once automatic writes are off");
}
