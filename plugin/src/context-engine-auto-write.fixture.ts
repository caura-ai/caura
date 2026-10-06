/** Isolated runtime regression fixture, launched by context-engine.test.ts. */
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CauraContextEngine,
  _resetSessionBuffersForTests,
  _sessionKeysForTests,
} from "./context-engine.js";
import { apiCall } from "./transport.js";

const enabled = process.argv[2] === "enabled";
const writes: Record<string, unknown>[] = [];
globalThis.fetch = async (input, init) => {
  assert.equal(new URL(String(input)).pathname, "/api/v1/memories");
  assert.equal(init?.method, "POST");
  writes.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
  return new Response(JSON.stringify({ id: "fixture-memory" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};

const runtimeRoot = await mkdtemp(join(tmpdir(), "caura-auto-write-"));
try {
  // Exercise real SDK discovery and delegation without starting OpenClaw.
  const launcher = join(runtimeRoot, "launcher.js");
  await writeFile(launcher, "// synthetic launcher\n");
  await writeFile(join(runtimeRoot, "package.json"), JSON.stringify({
    name: "openclaw",
    type: "module",
    exports: { "./plugin-sdk": "./sdk.js" },
  }));
  await writeFile(join(runtimeRoot, "sdk.js"), `
    export async function delegateCompactionToRuntime(context) {
      return { ok: true, compacted: true, result: { sessionId: context.sessionId } };
    }
  `);
  process.argv[1] = launcher;

  const engine = new CauraContextEngine({ agentId: "writer", sessionId: "fixture" });
  // Bootstrap's synthetic connectivity probe is not conversation persistence.
  engine.bootstrap = async () => {};
  _resetSessionBuffersForTests();
  const userMessage = "A user message that should only persist when automatic writing is enabled. ".repeat(3);
  const assistantMessage = "An assistant turn summary with enough content to pass the substantive-turn gate. ".repeat(3);
  const compactionSummary = "A summary of the conversation for compaction.";

  await engine.ingest({ role: "user", content: userMessage });
  assert.equal(_sessionKeysForTests().length, 1, "local buffering survives the opt-out");
  await engine.afterTurn({ messages: [{ role: "assistant", content: assistantMessage }] });
  for (const summaryField of ["summary", "compactionSummary"]) {
    const result = await engine.compact({
      [summaryField]: compactionSummary,
      sessionId: "fixture-session",
      sessionFile: join(runtimeRoot, "session.jsonl"),
    });
    assert.deepEqual(result, {
      ok: true,
      compacted: true,
      result: { sessionId: "fixture-session" },
    }, "runtime compaction still delegates when persistence is disabled");
  }

  assert.equal(writes.length, enabled ? 4 : 0);
  if (enabled) {
    assert.deepEqual(writes.map(w => w.content), [
      userMessage, assistantMessage, compactionSummary, compactionSummary,
    ]);
    assert.deepEqual(writes.map(w => (w.metadata as { tags: string[] }).tags), [
      ["auto-ingest", "user-message"], ["auto-turn-summary"],
      ["auto-compaction"], ["auto-compaction"],
    ]);
  }

  await apiCall("POST", "/memories", {
    tenant_id: "auto-write-fixture",
    agent_id: "writer",
    content: "An explicit memory write remains available.",
  });
  assert.equal(writes.length, enabled ? 5 : 1);
} finally {
  await rm(runtimeRoot, { recursive: true, force: true });
}
