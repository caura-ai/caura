import { test } from "node:test";
import assert from "node:assert/strict";

import { MemClaw, MemClawApiError, AuthError, NotFoundError } from "./index.js";

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

function jsonResponse(status: number, data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeClient(handler: Handler, options: Record<string, unknown> = {}): MemClaw {
  return new MemClaw("mc_test", {
    tenantId: "t1",
    baseUrl: "https://example.test",
    fetch: ((url: string, init: RequestInit) => Promise.resolve(handler(url, init))) as typeof fetch,
    ...options,
  });
}

test("write posts to /memories and parses the response", async () => {
  const client = makeClient((url, init) => {
    assert.equal(new URL(url).pathname, "/api/v1/memories");
    assert.equal((init.headers as Record<string, string>)["X-API-Key"], "mc_test");
    assert.deepEqual(JSON.parse(init.body as string), {
      tenant_id: "t1",
      content: "hello",
      agent_id: "a1",
    });
    return jsonResponse(201, { id: "m1", content: "hello", title: "Hi", agent_id: "a1" });
  }, { agentId: "a1" });

  const mem = await client.write("hello");
  assert.equal(mem.id, "m1");
  assert.equal(mem.title, "Hi");
  assert.equal(mem.raw.agent_id, "a1");
});

test("write per-call agentId overrides the default", async () => {
  const client = makeClient((_url, init) => {
    assert.equal(JSON.parse(init.body as string).agent_id, "override");
    return jsonResponse(201, { id: "m1", content: "x" });
  }, { agentId: "default" });
  await client.write("x", { agentId: "override" });
});

test("search posts to /search and returns a list", async () => {
  const client = makeClient((url, init) => {
    assert.equal(new URL(url).pathname, "/api/v1/search");
    const body = JSON.parse(init.body as string);
    assert.equal(body.query, "q");
    assert.equal(body.top_k, 3);
    return jsonResponse(200, { items: [{ id: "m1", content: "a" }, { id: "m2", content: "b" }] });
  });
  const results = await client.search("q", { topK: 3 });
  assert.deepEqual(results.map((m) => m.id), ["m1", "m2"]);
});

test("recall returns the summary and supporting memories", async () => {
  const client = makeClient((url) => {
    assert.equal(new URL(url).pathname, "/api/v1/recall");
    return jsonResponse(200, { summary: "S", supporting_memories: [{ id: "m1", content: "a" }] });
  });
  const result = await client.recall("q");
  assert.equal(result.summary, "S");
  assert.equal(result.supportingMemories[0].id, "m1");
});

test("health hits /health", async () => {
  const client = makeClient((url) => {
    assert.equal(new URL(url).pathname, "/api/v1/health");
    return jsonResponse(200, { status: "ok" });
  });
  assert.equal((await client.health()).status, "ok");
});

test("403 maps to AuthError and parses the error envelope", async () => {
  const client = makeClient(() => jsonResponse(403, { error: { message: "cross-fleet", details: { x: 1 } } }));
  await assert.rejects(client.write("x"), (err: unknown) => {
    assert.ok(err instanceof AuthError);
    assert.equal((err as AuthError).statusCode, 403);
    assert.deepEqual((err as AuthError).details, { x: 1 });
    return true;
  });
});

test("404 maps to NotFoundError", async () => {
  const client = makeClient(() => jsonResponse(404, { detail: "nope" }));
  await assert.rejects(client.search("q"), NotFoundError);
});

test("500 maps to MemClawApiError", async () => {
  const client = makeClient(() => jsonResponse(500, { message: "boom" }));
  await assert.rejects(client.recall("q"), MemClawApiError);
});

test("constructor validates apiKey and tenantId", () => {
  assert.throws(() => new MemClaw("", { tenantId: "t" }));
  assert.throws(() => new MemClaw("k", { tenantId: "" } as never));
});
