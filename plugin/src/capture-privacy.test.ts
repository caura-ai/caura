/**
 * What automatic capture puts back into a conversation.
 *
 * One agent serves many conversations (``agent:NAME:CHANNEL:...``). A turn the
 * plugin captured on its own belongs to the conversation it came from: the
 * server records that conversation on the row (``system_metadata.capture``),
 * and the plugin's automatic recall injects such a row only into the same
 * conversation. Rows the agent wrote deliberately are its knowledge and are
 * recalled anywhere, as before. Captured rows that reach the prompt are marked
 * as captured, so the model can tell them from deliberate writes.
 *
 * Separate file because CAURA_* settings are read at env.js import time
 * (node --test runs each file in its own process).
 */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";

process.env.CAURA_API_URL = "https://capture-privacy.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-capture-privacy";
process.env.CAURA_RECALL_POLICY = "always";
process.env.CAURA_KEYSTONES_ENABLED = "false";
process.env.CAURA_INTERVIEWER = "false";
delete process.env.CAURA_RECALL_CROSS_AGENT;

const { CauraContextEngine, formatRecallBlock } = await import("./context-engine.js");

// The conversation id the server stamps: the first 32 hex characters of the
// SHA-256 of the session header the plugin sends (``openclaw:<sessionKey>``).
// core-api's tests pin the same two vectors.
const DM = { key: "agent:main:telegram:dm:42", id: "ef394477dbfa5bfa49682d369231cb10" };
const GROUP = { key: "agent:main:whatsapp:group:7", id: "0608fced8880c8f4cf956d61f370d9fa" };

const ROWS = [
  {
    id: "11111111-1111-4111-8111-111111111111",
    memory_type: "decision",
    content: "DELIBERATE: deploys go out blue-green",
    metadata: { tags: ["deploy"] },
  },
  {
    id: "22222222-2222-4222-8222-222222222222",
    memory_type: "episode",
    content: "SAME-CONVERSATION: we agreed to ship on Friday",
    metadata: { tags: ["auto-turn-summary"] },
    system_metadata: { capture: { source: "plugin", trigger: "auto-turn-summary", conversation: DM.id } },
  },
  {
    id: "33333333-3333-4333-8333-333333333333",
    memory_type: "episode",
    content: "OTHER-CONVERSATION: my card ends in 4242",
    metadata: { tags: ["auto-ingest", "user-message"] },
    system_metadata: { capture: { source: "plugin", trigger: "auto-ingest", conversation: GROUP.id } },
  },
  {
    // Captured before the server recorded conversations: it cannot be placed.
    id: "44444444-4444-4444-8444-444444444444",
    memory_type: "episode",
    content: "UNPLACED: an old captured turn",
    metadata: { tags: ["auto-turn-summary"] },
  },
];

let searches: Array<Record<string, unknown>> = [];
let realFetch: typeof globalThis.fetch;

before(() => {
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    let payload: unknown = { ok: true };
    if (path.endsWith("/search")) {
      searches.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      payload = { items: ROWS, recall_tracked: true };
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
});

after(() => {
  globalThis.fetch = realFetch;
});

async function recalledInto(sessionKey: string): Promise<string> {
  searches = [];
  const engine = new CauraContextEngine({});
  engine.bootstrap = async () => {};
  // The same question in both conversations: a cached recall must not carry
  // one conversation's rows into the other.
  const prompt = "what did we decide about the deploy?";
  const out = await engine.assemble({
    sessionKey,
    prompt,
    messages: [{ role: "user", content: prompt }],
  });
  assert.equal(searches.length, 1, "assemble ran no recall");
  return String(out.systemPromptAddition ?? "");
}

test("a captured row is recalled only into the conversation it came from", async () => {
  const block = await recalledInto(DM.key);
  assert.match(block, /DELIBERATE/);
  assert.match(block, /SAME-CONVERSATION/);
  assert.doesNotMatch(block, /OTHER-CONVERSATION/);
  assert.doesNotMatch(block, /UNPLACED/);

  const other = await recalledInto(GROUP.key);
  assert.match(other, /OTHER-CONVERSATION/);
  assert.doesNotMatch(other, /SAME-CONVERSATION/);
});

test("captured rows are marked in the recalled block, deliberate ones are not", () => {
  const block = formatRecallBlock([ROWS[0], ROWS[1]]);
  const lines = block.split("\n");
  const deliberate = lines.find((l) => l.includes("DELIBERATE")) ?? "";
  const captured = lines.find((l) => l.includes("SAME-CONVERSATION")) ?? "";
  assert.doesNotMatch(deliberate, /auto-captured/);
  assert.match(captured, /^- \[episode, auto-captured\] /);
});
