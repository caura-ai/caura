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

// The supported callback carries no trusted conversation identity. Both the
// default-on and opt-out configurations must refuse the automatic flush turn.
const plan = resolveFlushPlan({ nowMs: Date.UTC(2026, 10, 9, 12) });
assert.equal(plan, null, "no flush plan without trustworthy per-conversation context (capture " +
  (enabled ? "enabled" : "disabled") + ")");
