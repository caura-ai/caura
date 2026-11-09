/** Isolated hook/transport tests, launched by context-engine.test.ts. */
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CauraContextEngine, type IngestMessage } from "./context-engine.js";
import { applyPluginConfig } from "./env.js";
import { apiCall } from "./transport.js";
import { __INTERVIEW_BUFFER_INTERNALS__, readInterviewEvents } from "./interview-buffer.js";

const mode = process.argv[2];
const writes: Array<{ body: Record<string, unknown>; session: string | null }> = [];
const substantive = "A synthetic conversation turn about scheduling a release and checking its documentation. ".repeat(3);
const directKey = "agent:writer:telegram:dm:42";
const engine = new CauraContextEngine({ agentId: "writer", sessionKey: directKey });
engine.bootstrap = async () => {};

globalThis.fetch = async (input, init) => {
  assert.equal(new URL(String(input)).pathname, "/api/v1/memories");
  assert.equal(init?.method, "POST");
  writes.push({
    body: JSON.parse(String(init?.body)) as Record<string, unknown>,
    session: new Headers(init?.headers).get("X-Caura-Session-Id"),
  });
  return new Response(JSON.stringify({ id: "synthetic-memory" }), {
    status: mode === "failed-attempt-cap" ? 503 : 200,
    headers: { "Content-Type": "application/json" },
  });
};

async function afterTurn(key: string, content = substantive, target = engine): Promise<void> {
  await target.afterTurn({ sessionKey: key, messages: [{ role: "assistant", content }] });
}

if (mode === "blocked") {
  const hook = process.argv[3];
  const context = JSON.parse(process.argv[4]) as Record<string, unknown>;
  if (hook === "ingest") {
    await engine.ingest({ ...context, role: "user", content: substantive } as IngestMessage);
  } else if (hook === "afterTurn") {
    await engine.afterTurn({ ...context, messages: [{ role: "assistant", content: substantive }] });
  } else {
    // Exercise runtime delegation too: blocking persistence must not disable
    // the host's ordinary compaction of a group conversation.
    const root = await mkdtemp(join(tmpdir(), "capture-bounds-"));
    try {
      const launcher = join(root, "launcher.js");
      await writeFile(launcher, "// synthetic launcher\n");
      await writeFile(join(root, "package.json"), JSON.stringify({
        name: "openclaw", type: "module", exports: { "./plugin-sdk": "./sdk.js" },
      }));
      await writeFile(join(root, "sdk.js"),
        "export async function delegateCompactionToRuntime() { return { ok: true, compacted: true }; }\n");
      process.argv[1] = launcher;
      for (const summaryField of ["summary", "compactionSummary"]) {
        const result = await engine.compact({
          ...context, [summaryField]: substantive,
          sessionId: "synthetic-session", sessionFile: join(root, "session.jsonl"),
        });
        assert.equal(result.compacted, true, "runtime compaction must remain available");
      }
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
  assert.equal(writes.length, 0, hook + " persisted a group or unknown-context conversation");
  await apiCall("POST", "/memories", {
    tenant_id: "auto-write-fixture", agent_id: "writer", content: "A deliberate memory write.",
  });
  assert.equal(writes.length, 1, "explicit memory writes remain available");
} else if (mode === "interview-bounds") {
  const root = await mkdtemp(join(tmpdir(), "capture-interview-bounds-"));
  __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(join(root, "events.jsonl"));
  try {
    // The independently enabled interviewer still works in eligible direct
    // conversations with ordinary auto-writes off, but cannot mirror a group.
    applyPluginConfig({ CAURA_AUTO_WRITE_TURNS: false });
    await engine.ingest({ ...JSON.parse(process.argv[3]), role: "user", content: substantive });
    const events = await readInterviewEvents(0, 10);
    assert.equal(events.length, Number(process.argv[4]));
    assert.equal(writes.length, 0);
  } finally {
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(undefined);
    await rm(root, { recursive: true, force: true });
  }
} else if (mode === "tracked-session-cap") {
  for (let i = 0; i < 100; i++) await afterTurn("agent:writer:telegram:dm:" + i);
  assert.equal(writes.length, 100);
  await afterTurn("agent:writer:telegram:dm:overflow");
  assert.equal(writes.length, 101, "the 101st conversation must still capture a turn");
  assert.equal(writes[100].session, "openclaw:agent:writer:telegram:dm:overflow");
  for (let i = 0; i < 14; i++) await afterTurn(directKey, substantive + i);
  assert.equal(writes.length, 110, "admitting a new conversation must preserve retained budgets");
} else if (mode === "oldest-session-budget") {
  const keyFor = (id: number) => "agent:writer:telegram:dm:" + id;
  // Fill in order, exhausting 0 and 2. No later access refreshes an earlier
  // conversation, so the resulting LRU order is still 0, 1, 2, ... 99.
  for (let i = 0; i < 100; i++) {
    for (let turn = 0; turn < (i === 0 || i === 2 ? 10 : 1); turn++) {
      await afterTurn(keyFor(i), substantive + turn);
    }
  }
  assert.equal(writes.length, 118);
  await afterTurn("agent:writer:telegram:dm:overflow");
  const beforeReturn = writes.length;
  // Accepted trade-off: the evicted least-recently-used conversation gets a fresh budget
  // on return. A different eviction victim or no eviction leaves it exhausted.
  for (let i = 0; i < 10; i++) await afterTurn(keyFor(0), substantive + i);
  assert.equal(writes.length, beforeReturn + 10, "the least-recently-used counter must be evicted");
  // Reintroducing 0 evicts 1, not the later retained and exhausted budget 2.
  await afterTurn(keyFor(2));
  assert.equal(writes.length, beforeReturn + 10, "eviction must not clear unrelated counters");
} else if (mode === "turn-budget-lru") {
  const keyFor = (id: number) => "agent:writer:telegram:dm:" + id;
  for (let i = 0; i < 100; i++) await afterTurn(keyFor(i));
  await afterTurn(keyFor(0)); // refresh the oldest-created conversation
  assert.equal(writes.length, 101);
  await afterTurn("agent:writer:telegram:dm:overflow");
  assert.equal(writes.length, 102);
  // A recent reservation must keep 0's existing two-write budget. FIFO
  // evicts 0 instead and incorrectly grants ten more writes on its return.
  for (let i = 0; i < 14; i++) await afterTurn(keyFor(0), substantive + i);
  assert.equal(writes.length, 110, "recently reserved conversations must retain their budget");
  // Conversation 1 is now least recently used, so it gets a fresh budget.
  for (let i = 0; i < 10; i++) await afterTurn(keyFor(1), substantive + i);
  assert.equal(writes.length, 120, "the idle conversation must be the eviction victim");
} else if (mode === "wrapper") {
  // This is the real OpenClaw call shape. It currently does not persist user
  // messages; bounding capture must not silently turn that path on.
  const key = process.argv[3];
  await engine.ingest({
    sessionId: "synthetic-session", sessionKey: key,
    message: { role: "user", content: substantive },
  } as unknown as IngestMessage);
  assert.equal(writes.length, 0, "wrapper-shaped ingest must not enable extra user-message capture");
} else if (mode === "session-cap") {
  for (let i = 0; i < 14; i++) await afterTurn(directKey, substantive + i);
  assert.equal(writes.length, 10, "afterTurn must stop at ten writes per session");
  const replacementEngine = new CauraContextEngine({ agentId: "writer" });
  replacementEngine.bootstrap = async () => {};
  await afterTurn(directKey, substantive, replacementEngine);
  assert.equal(writes.length, 10, "recreating the engine must not reset the session budget");
  const otherKey = "agent:writer:telegram:dm:99";
  await afterTurn(otherKey);
  assert.equal(writes.length, 11, "another direct conversation has its own budget");
  assert.equal(writes[10].session, "openclaw:" + otherKey);
  assert.ok(writes.every(w => w.body.visibility === "scope_agent"));
} else if (mode === "concurrent-cap") {
  await Promise.all(Array.from({ length: 24 }, (_, i) => afterTurn(directKey, substantive + i)));
  assert.equal(writes.length, 10, "concurrent hooks must reserve budget before awaiting a write");
} else if (mode === "short-turns") {
  for (let i = 0; i < 14; i++) await afterTurn(directKey, "ok");
  assert.equal(writes.length, 0);
  for (let i = 0; i < 10; i++) await afterTurn(directKey, substantive + i);
  assert.equal(writes.length, 10, "short turns must not spend the substantive-write budget");
} else if (mode === "failed-attempt-cap") {
  for (let i = 0; i < 14; i++) await afterTurn(directKey, substantive + i);
  assert.equal(writes.length, 10, "failed writes must not bypass the per-session request bound");
} else if (mode === "config-opt-out") {
  applyPluginConfig({ CAURA_AUTO_WRITE_TURNS: "OFF" });
  await afterTurn(directKey);
  assert.equal(writes.length, 0);
  applyPluginConfig({ CAURA_AUTO_WRITE_TURNS: true });
  await afterTurn(directKey);
  assert.equal(writes.length, 1, "capture uses the effective config binding, not its import-time value");
} else {
  throw new Error("unknown fixture mode: " + mode);
}
