/**
 * Identity wiring for the tools that send ``agent_id`` (SAFE-04B / F5).
 *
 * Three dispatches used to invent their own fallback and disagree.
 * ``caura_write`` fell back to ``main-${installId}``; ``caura_manage``
 * op=update and ``caura_tune`` fell back to the literal ``"unknown-agent"``.
 *
 * The literal is not an identity, and the two tools carrying it fail
 * differently, which is why only one of them was noticed:
 *
 *   - ``caura_tune`` PATCHes ``/agents/unknown-agent/tune`` and 404s. Loud.
 *   - ``caura_manage`` op=update sends it as the ``agent_id`` QUERY PARAM,
 *     where ``routes/memories.py`` resolves ``auth.agent_id or agent_id`` and
 *     feeds the result to trust and fleet enforcement. With no gateway agent
 *     header that literal becomes the authorizing principal. Silent.
 *
 * These assert on the request the server would actually receive, so a
 * regression in either the resolver or a dispatch's own wiring is caught.
 */
import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";

// Env is read into module constants at import time, so it must be set before
// the dynamic imports below. No CAURA_AGENT_ID: that is the case under test.
process.env.CAURA_API_URL = "http://identity.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-identity";
delete process.env.CAURA_AGENT_ID;
delete process.env.MEMCLAW_AGENT_ID; // legacy-name-ok: the dual-read alias must be cleared too

const { createToolFromSpec } = await import("./tool-definitions.js");
const { resolveAgentIdQuiet } = await import("./resolve-agent.js");

/** The identity every one of these tools must converge on. */
const EXPECTED_AGENT_ID = resolveAgentIdQuiet({});

interface Captured {
  url: URL;
  method: string;
  body: Record<string, unknown> | undefined;
}

let captured: Captured[] = [];
let realFetch: typeof globalThis.fetch;

before(() => {
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured.push({
      url: new URL(String(input)),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
});

after(() => {
  globalThis.fetch = realFetch;
});

/**
 * The operation request, not the credential lookup.
 *
 * ``apiCall`` resolves an agent-scoped API key before issuing the call
 * whenever an ``agent_id`` is in play, so an identity-bearing tool makes two
 * requests and the operation is always the last. Asserting on a fixed count
 * would make these tests fail the day a tool grows another round-trip, which
 * is not what they are guarding.
 */
async function run(tool: string, params: Record<string, unknown>): Promise<Captured> {
  captured = [];
  await createToolFromSpec(tool).execute("test-call", params, undefined as never);
  assert.ok(captured.length >= 1, `${tool} made no request`);
  return captured[captured.length - 1];
}

describe("agent identity is resolved, never invented", () => {
  test("the resolved default is a real install-scoped id", () => {
    assert.match(EXPECTED_AGENT_ID, /^main-.+/);
    assert.notEqual(EXPECTED_AGENT_ID, "unknown-agent");
  });

  test("caura_tune targets the resolved agent, not a placeholder path", async () => {
    const req = await run("caura_tune", { weight_delta: 0.1, memory_id: "m-1" });
    assert.equal(
      req.url.pathname,
      `/api/v1/agents/${encodeURIComponent(EXPECTED_AGENT_ID)}/tune`,
    );
    assert.doesNotMatch(req.url.pathname, /unknown-agent/);
  });

  test("caura_manage op=update authorizes as the resolved agent", async () => {
    const req = await run("caura_manage", {
      op: "update",
      memory_id: "11111111-1111-4111-8111-111111111111",
      content: "updated",
    });
    assert.equal(req.method, "PATCH");
    assert.equal(req.url.searchParams.get("agent_id"), EXPECTED_AGENT_ID);
    assert.notEqual(req.url.searchParams.get("agent_id"), "unknown-agent");
  });

  test("caura_write sends the same identity as the other two", async () => {
    const req = await run("caura_write", { content: "hello" });
    assert.equal(req.body?.agent_id, EXPECTED_AGENT_ID);
  });

  test("an explicit agent_id always wins over the default", async () => {
    const req = await run("caura_tune", {
      agent_id: "explicit-agent",
      weight_delta: 0.1,
      memory_id: "m-1",
    });
    assert.equal(req.url.pathname, "/api/v1/agents/explicit-agent/tune");
  });

  test("reads still send no agent_id — identity resolution must not narrow scope", async () => {
    const req = await run("caura_recall", { query: "anything" });
    assert.equal(req.body?.agent_id, undefined);
    assert.equal(req.url.searchParams.get("agent_id"), null);
  });
});
