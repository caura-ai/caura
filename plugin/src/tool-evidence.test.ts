/**
 * Evidence contract v1 (EC-15): what ``caura_write`` tells the server about
 * where a memory came from.
 *
 * - The OpenClaw session the tool runs in goes out as ``X-Caura-Session-Id``,
 *   the header the server stamps ``trace_id`` from. A header, so a server that
 *   predates the evidence fields ignores it rather than 422-ing the write.
 * - ``tool_call_id`` / ``observed_at`` / ``trace_id`` are the agent's to send;
 *   on a batch they belong inside each item, so the top-level ones are dropped
 *   like the other single-write-only fields.
 * - The id of the ``caura_write`` call itself is never sent as
 *   ``tool_call_id``: it is not the call the fact was observed in.
 */
import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";

process.env.CAURA_API_URL = "https://evidence.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-evidence";
process.env.CAURA_AGENT_ID = "configured-agent";

const { createToolFromSpec } = await import("./tool-definitions.js");

interface Captured {
  path: string;
  body: Record<string, unknown> | undefined;
  session: string | null;
}

let captured: Captured[] = [];
let realFetch: typeof globalThis.fetch;

before(() => {
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured.push({
      path: new URL(String(input)).pathname,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      session: new Headers(init?.headers).get("X-Caura-Session-Id"),
    });
    return new Response(JSON.stringify({ id: "m1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
});

after(() => {
  globalThis.fetch = realFetch;
});

async function write(
  params: Record<string, unknown>,
  run?: { sessionKey?: string; sessionId?: string },
): Promise<Captured> {
  captured = [];
  await createToolFromSpec("caura_write", run).execute("call_of_the_write_itself", params);
  const op = captured.filter((c) => c.path.endsWith("/memories") || c.path.endsWith("/memories/bulk"));
  assert.equal(op.length, 1, "one write request");
  return op[0];
}

describe("caura_write evidence", () => {
  test("a write carries the OpenClaw session it was made in", async () => {
    const req = await write(
      { agent_id: "a1", content: "the deploy window moved to thursday" },
      { sessionKey: "agent:a1:telegram:dm:42" },
    );
    assert.equal(req.session, "openclaw:agent:a1:telegram:dm:42");
  });

  test("the session id stands in when OpenClaw names no session key", async () => {
    const req = await write({ agent_id: "a1", content: "x".repeat(20) }, { sessionId: "uuid-1" });
    assert.equal(req.session, "openclaw:uuid-1");
  });

  test("a batch carries the session too", async () => {
    const req = await write(
      { agent_id: "a1", items: [{ content: "x".repeat(20) }] },
      { sessionKey: "agent:a1:main" },
    );
    assert.ok(req.path.endsWith("/memories/bulk"));
    assert.equal(req.session, "openclaw:agent:a1:main");
  });

  test("no run, no session header", async () => {
    const req = await write({ agent_id: "a1", content: "x".repeat(20) });
    assert.equal(req.session, null);
  });

  test("the agent's evidence goes through, and the write's own call id never does", async () => {
    const req = await write({
      agent_id: "a1",
      content: "x".repeat(20),
      tool_call_id: "toolu_01ABC",
      observed_at: "2026-11-01T09:30:00Z",
    });
    assert.equal(req.body?.tool_call_id, "toolu_01ABC");
    assert.equal(req.body?.observed_at, "2026-11-01T09:30:00Z");

    const plain = await write({ agent_id: "a1", content: "x".repeat(20) });
    assert.equal("tool_call_id" in (plain.body ?? {}), false);
    assert.equal(JSON.stringify(plain.body).includes("call_of_the_write_itself"), false);
  });

  test("on a batch the evidence lives in the items, not at the top level", async () => {
    const req = await write({
      agent_id: "a1",
      tool_call_id: "stray",
      observed_at: "2026-11-01T09:30:00Z",
      trace_id: "stray",
      items: [{ content: "x".repeat(20), tool_call_id: "toolu_item" }],
    });
    for (const k of ["tool_call_id", "observed_at", "trace_id"]) {
      assert.equal(k in (req.body ?? {}), false, k);
    }
    const [item] = req.body?.items as Array<Record<string, unknown>>;
    assert.equal(item.tool_call_id, "toolu_item");
  });

  test("the tool schema offers the evidence fields, single and per item", () => {
    const write = createToolFromSpec("caura_write").parameters as {
      properties: Record<string, unknown> & { items: { items: { properties: Record<string, unknown> } } };
    };
    for (const k of ["tool_call_id", "observed_at", "trace_id", "source_uri"]) {
      assert.ok(write.properties[k], `single: ${k}`);
      assert.ok(write.properties.items.items.properties[k], `item: ${k}`);
    }
  });
});
