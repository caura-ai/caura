import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  Caura,
  CauraError,
  TransportError,
  CauraApiError,
  AuthError,
  NotFoundError,
  RateLimitError,
  USER_AGENT,
  VERSION,
} from "./index.js";

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

function jsonResponse(status: number, data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stalledJsonResponse(signal: AbortSignal, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () =>
      new Promise<never>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      }),
  } as unknown as Response;
}

function makeClient(handler: Handler, options: Record<string, unknown> = {}): Caura {
  return new Caura("mc_test", {
    tenantId: "t1",
    baseUrl: "https://example.test",
    fetch: ((url: string, init: RequestInit) => Promise.resolve(handler(url, init))) as typeof fetch,
    ...options,
  });
}

test("VERSION agrees with package.json", () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(VERSION, pkg.version);
});

test("every request names the SDK in User-Agent", async () => {
  let seen: string | undefined;
  const client = makeClient((_url, init) => {
    seen = (init.headers as Record<string, string>)["User-Agent"];
    return jsonResponse(200, { status: "ok" });
  });
  await client.health();
  assert.equal(seen, USER_AGENT);
  assert.equal(seen, `caura-client-node/${VERSION} (node/${process.versions.node.split(".")[0]})`);
  assert.match(seen!, /^caura-client-node\/\d+\.\d+\.\d+ \(node\/\d+\)$/);
});

test("write posts to /memories and parses the response", async () => {
  const client = makeClient(
    (url, init) => {
      assert.equal(new URL(url).pathname, "/api/v1/memories");
      assert.equal((init.headers as Record<string, string>)["X-API-Key"], "mc_test");
      assert.deepEqual(JSON.parse(init.body as string), {
        tenant_id: "t1",
        content: "hello",
        agent_id: "a1",
      });
      return jsonResponse(201, { id: "m1", content: "hello", title: "Hi", agent_id: "a1" });
    },
    { agentId: "a1" },
  );

  const mem = await client.write("hello");
  assert.equal(mem.id, "m1");
  assert.equal(mem.title, "Hi");
  assert.equal(mem.raw.agent_id, "a1");
});

test("write per-call agentId overrides the default", async () => {
  const client = makeClient(
    (_url, init) => {
      assert.equal(JSON.parse(init.body as string).agent_id, "override");
      return jsonResponse(201, { id: "m1", content: "x" });
    },
    { agentId: "default" },
  );
  await client.write("x", { agentId: "override" });
});

test("search posts to /search and returns a list", async () => {
  const client = makeClient((url, init) => {
    assert.equal(new URL(url).pathname, "/api/v1/search");
    const body = JSON.parse(init.body as string);
    assert.equal(body.query, "q");
    assert.equal(body.top_k, 3);
    return jsonResponse(200, {
      items: [
        { id: "m1", content: "a" },
        { id: "m2", content: "b" },
      ],
    });
  });
  const results = await client.search("q", { topK: 3 });
  assert.deepEqual(
    results.map((m) => m.id),
    ["m1", "m2"],
  );
});

test("search throws when 200 body lacks items", async () => {
  const client = makeClient(() => jsonResponse(200, { error: "quota exceeded" }));
  await assert.rejects(client.search("q"), (err: unknown) => {
    assert.ok(err instanceof CauraApiError);
    assert.equal((err as CauraApiError).statusCode, 200);
    assert.equal((err as CauraApiError).message, '[200] search response missing "items" list');
    return true;
  });
});

test("search throws when 200 items is not a list", async () => {
  const client = makeClient(() => jsonResponse(200, { items: "not-a-list" }));
  await assert.rejects(client.search("q"), (err: unknown) => {
    assert.ok(err instanceof CauraApiError);
    assert.equal((err as CauraApiError).statusCode, 200);
    assert.equal((err as CauraApiError).message, '[200] search response "items" must be a list');
    return true;
  });
});

// The exact top-level key set POST /api/v1/recall returns, per
// core_api.services.recall_service.summarize_memories — pinned server-side by
// tests/test_c4_recall_items_alias.py::_EXPECTED_TOP_LEVEL_KEYS. Note what is NOT
// here: `supporting_memories`. H-01 was this SDK reading that invented key with a
// fixture that mocked it, so CI passed while every live recall() returned nothing.
function liveRecallBody(memories: Array<Record<string, unknown>>) {
  return {
    query: "q",
    summary: "S",
    memory_count: memories.length,
    memories,
    items: memories, // server aliases the identical list
    recall_ms: 12,
  };
}

test("recall returns the summary and supporting memories", async () => {
  const client = makeClient((url) => {
    assert.equal(new URL(url).pathname, "/api/v1/recall");
    return jsonResponse(200, liveRecallBody([{ id: "m1", content: "a" }]));
  });
  const result = await client.recall("q");
  assert.equal(result.summary, "S");
  assert.equal(result.supportingMemories[0].id, "m1");
});

test("recall accepts the items alias alone", async () => {
  const client = makeClient(() => jsonResponse(200, { summary: "S", items: [{ id: "m2", content: "b" }] }));
  const result = await client.recall("q");
  assert.deepEqual(
    result.supportingMemories.map((m) => m.id),
    ["m2"],
  );
});

test("recall with no memories is empty, not an error", async () => {
  const client = makeClient(() => jsonResponse(200, liveRecallBody([])));
  const result = await client.recall("q");
  assert.deepEqual(result.supportingMemories, []);
  assert.equal(result.summary, "S");
});

test("recall ignores the key the server never sends", async () => {
  // Guard against the regression: a body carrying ONLY the invented key must
  // yield no memories, so nobody "fixes" this by reinstating it.
  const client = makeClient(() =>
    jsonResponse(200, { summary: "S", supporting_memories: [{ id: "ghost" }] }),
  );
  const result = await client.recall("q");
  assert.deepEqual(result.supportingMemories, []);
});

test("recall throws when 200 body is not an object", async () => {
  const client = makeClient(() => jsonResponse(200, ["not", "a", "dict"]));
  await assert.rejects(client.recall("q"), (err: unknown) => {
    assert.ok(err instanceof CauraApiError);
    assert.equal((err as CauraApiError).statusCode, 200);
    assert.equal((err as CauraApiError).message, "[200] recall response must be a JSON object");
    return true;
  });
});

test("recall throws when 200 body is a bare scalar", async () => {
  const client = makeClient(() => jsonResponse(200, "not an object"));
  await assert.rejects(client.recall("q"), (err: unknown) => {
    assert.ok(err instanceof CauraApiError);
    assert.equal((err as CauraApiError).statusCode, 200);
    assert.equal((err as CauraApiError).message, "[200] recall response must be a JSON object");
    return true;
  });
});

test("recall translates topK and forwards only the extras", async () => {
  let captured: Record<string, unknown> = {};
  const client = makeClient((_url, init) => {
    captured = JSON.parse(init.body as string);
    return jsonResponse(200, liveRecallBody([]));
  });
  await client.recall("q", { topK: 7, diagnostic: true });
  assert.equal(captured.top_k, 7);
  assert.equal(captured.diagnostic, true);
  assert.equal("topK" in captured, false);
});

test("health hits /health", async () => {
  const client = makeClient((url) => {
    assert.equal(new URL(url).pathname, "/api/v1/health");
    return jsonResponse(200, { status: "ok" });
  });
  assert.equal((await client.health()).status, "ok");
});

test("getDocument fetches document by id with collection and tenant query params", async () => {
  const client = makeClient((url, init) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/api/v1/documents/doc-1");
    assert.equal(parsed.searchParams.get("tenant_id"), "t1");
    assert.equal(parsed.searchParams.get("collection"), "interviews");
    assert.equal(init.method, "GET");
    assert.equal((init.headers as Record<string, string>)["X-API-Key"], "mc_test");
    return jsonResponse(200, { id: "doc-1", data: { title: "Doc Title" } });
  });

  const doc = await client.getDocument("doc-1", { collection: "interviews" });
  assert.equal(doc.id, "doc-1");
  assert.deepEqual(doc.data, { title: "Doc Title" });
});

test("getDocument encodes special characters in docId preventing path and query injection", async () => {
  const client = makeClient((url) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/api/v1/documents/path%2Fwith%2Fslash%3Fand%3Dquery");
    assert.equal(parsed.searchParams.get("tenant_id"), "t1");
    assert.equal(parsed.searchParams.get("collection"), "interviews");
    assert.equal(parsed.searchParams.has("and"), false);
    return jsonResponse(200, { id: "path/with/slash?and=query", data: {} });
  });

  await client.getDocument("path/with/slash?and=query", { collection: "interviews" });
});

test("getDocument allows overriding tenantId", async () => {
  const client = makeClient((url) => {
    const parsed = new URL(url);
    assert.equal(parsed.searchParams.get("tenant_id"), "override-tenant");
    assert.equal(parsed.searchParams.get("collection"), "interviews");
    return jsonResponse(200, { id: "doc-1", data: {} });
  });

  await client.getDocument("doc-1", { collection: "interviews", tenantId: "override-tenant" });
});

test("getDocument maps 404 to NotFoundError", async () => {
  const client = makeClient(() => jsonResponse(404, { detail: "document not found" }));
  await assert.rejects(client.getDocument("missing-id", { collection: "interviews" }), NotFoundError);
});

test("403 maps to AuthError and parses the error envelope", async () => {
  const client = makeClient(() =>
    jsonResponse(403, { error: { message: "cross-fleet", details: { x: 1 } } }),
  );
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

test("429 maps to RateLimitError and parses retry-after", async () => {
  const client = makeClient(
    async () =>
      new Response(JSON.stringify({ detail: "slow down" }), {
        status: 429,
        headers: { "content-type": "application/json", "retry-after": "2.5" },
      }),
  );
  await assert.rejects(
    client.search("q"),
    (err: unknown) => err instanceof RateLimitError && err.retryAfter === 2.5,
  );
});

test("429 without retry-after has null retryAfter", async () => {
  const client = makeClient(
    async () =>
      new Response(JSON.stringify({ detail: "slow down" }), {
        status: 429,
        headers: { "content-type": "application/json" },
      }),
  );
  await assert.rejects(
    client.search("q"),
    (err: unknown) => err instanceof RateLimitError && err.retryAfter === null,
  );
});

test("500 maps to CauraApiError", async () => {
  const client = makeClient(() => jsonResponse(500, { message: "boom" }));
  await assert.rejects(client.recall("q"), CauraApiError);
});

test("constructor validates apiKey and tenantId", () => {
  assert.throws(() => new Caura("", { tenantId: "t" }));
  assert.throws(() => new Caura("k", { tenantId: "" } as never));
});

for (const operation of ["write", "search", "recall", "health", "getDocument"] as const) {
  test(`${operation} wraps fetch failures and preserves the cause`, async () => {
    const cause = new TypeError("fetch failed");
    let calls = 0;
    const client = makeClient(() => {
      calls++;
      return Promise.reject(cause);
    });
    const request =
      operation === "getDocument"
        ? client.getDocument("doc-1", { collection: "interviews" })
        : client[operation]("query");
    await assert.rejects(request, (error: unknown) => {
      assert.ok(error instanceof CauraError);
      assert.ok(error instanceof TransportError);
      assert.equal(error.cause, cause);
      assert.match(error.message, /fetch failed/);
      return true;
    });
    assert.equal(calls, 1);
  });
}

test("the configured timeout wraps the abort reason", { timeout: 1000 }, async () => {
  let signal: AbortSignal | null | undefined;
  const client = makeClient(
    (_url, init) => {
      signal = init.signal;
      return new Promise<Response>((_resolve, reject) => {
        signal!.addEventListener("abort", () => reject(signal!.reason), { once: true });
      });
    },
    { timeoutMs: 0 },
  );
  await assert.rejects(client.search("query"), (error: unknown) => {
    assert.ok(signal?.aborted);
    assert.ok(error instanceof CauraError);
    assert.ok(error instanceof TransportError);
    assert.equal(error.cause, signal.reason);
    return true;
  });
});

for (const [status, phase] of [
  [200, "successful response bodies"],
  [500, "error response bodies"],
] as const) {
  test(`the configured timeout also covers ${phase}`, { timeout: 1000 }, async () => {
    let signal: AbortSignal | null | undefined;
    const client = makeClient(
      (_url, init) => {
        signal = init.signal;
        return stalledJsonResponse(signal!, status);
      },
      { timeoutMs: 0 },
    );
    await assert.rejects(client.search("query"), (error: unknown) => {
      assert.ok(signal?.aborted);
      assert.ok(error instanceof TransportError);
      assert.equal(error.cause, signal.reason);
      return true;
    });
  });
}

test("transport mapping does not wrap serialization errors", async () => {
  const client = makeClient(() => assert.fail("serialization must fail before fetch"));
  const circular: Record<string, unknown> = {};
  circular.self = circular;
  await assert.rejects(client.write("hello", { metadata: circular }), TypeError);
});

test("transport mapping does not wrap invalid JSON", async () => {
  const client = makeClient(() => new Response("not json"));
  await assert.rejects(client.health(), SyntaxError);
});

// L-66: the key never crosses the network in cleartext unless the caller opts in.

function clientFor(baseUrl: string, options: Record<string, unknown> = {}): Caura {
  return makeClient(() => jsonResponse(200, { status: "ok" }), { baseUrl, ...options });
}

function withEnvOptIn<T>(value: string | undefined, run: () => T): T {
  const saved = process.env.CAURA_ALLOW_INSECURE_HTTP;
  if (value === undefined) delete process.env.CAURA_ALLOW_INSECURE_HTTP;
  else process.env.CAURA_ALLOW_INSECURE_HTTP = value;
  try {
    return run();
  } finally {
    if (saved === undefined) delete process.env.CAURA_ALLOW_INSECURE_HTTP;
    else process.env.CAURA_ALLOW_INSECURE_HTTP = saved;
  }
}

test("plain http to a remote host is refused", () => {
  withEnvOptIn(undefined, () => {
    assert.throws(
      () => clientFor("http://caura.example"),
      (err: unknown) => {
        assert.ok(err instanceof Error);
        assert.match(err.message, /caura\.example/);
        assert.match(err.message, /allowInsecureHttp/);
        assert.match(err.message, /CAURA_ALLOW_INSECURE_HTTP/);
        return true;
      },
    );
  });
});

for (const scheme of ["ftp", "ws", "file"]) {
  test(`a ${scheme}:// base URL is refused`, () => {
    withEnvOptIn(undefined, () => {
      assert.throws(() => clientFor(`${scheme}://caura.example`), /https:\/\//);
    });
  });
}

for (const baseUrl of [
  "https://caura.example",
  "http://localhost:8000",
  "http://LOCALHOST:8000",
  "http://api.localhost",
  "http://127.0.0.1:8000",
  "http://127.8.9.10",
  "http://[::1]:8000",
]) {
  test(`${baseUrl} is allowed`, async () => {
    const client = withEnvOptIn(undefined, () => clientFor(baseUrl));
    assert.deepEqual(await client.health(), { status: "ok" });
  });
}

test("allowInsecureHttp allows plain http", () => {
  withEnvOptIn(undefined, () => clientFor("http://caura.example", { allowInsecureHttp: true }));
});

for (const value of ["true", "1"]) {
  test(`CAURA_ALLOW_INSECURE_HTTP=${value} allows plain http`, () => {
    withEnvOptIn(value, () => clientFor("http://caura.example"));
  });
}

for (const value of ["", "false", "0", "yes"]) {
  test(`CAURA_ALLOW_INSECURE_HTTP=${JSON.stringify(value)} does not opt in`, () => {
    withEnvOptIn(value, () => {
      assert.throws(() => clientFor("http://caura.example"));
    });
  });
}

test("an explicit allowInsecureHttp: false beats the env opt-in", () => {
  withEnvOptIn("true", () => {
    assert.throws(() => clientFor("http://caura.example", { allowInsecureHttp: false }));
  });
});

test("requests refuse redirects, so the key is never re-sent to a redirect target", async () => {
  let redirect: RequestRedirect | undefined;
  const client = makeClient((_url, init) => {
    redirect = init.redirect;
    return jsonResponse(200, { status: "ok" });
  });
  await client.health();
  assert.equal(redirect, "error");
});

// What search() and recall() send and return (audit 2026-10-01, B33 and B35).
// L-173: recall asks for its list once (`items_alias: false`). L-94: search keeps
// the envelope around its results. L-03: `callerAgentId` is an opt-in option of
// both.

test("L-173: recall asks for the list once unless told otherwise", async () => {
  const bodies: Array<Record<string, unknown>> = [];
  const client = makeClient((_url, init) => {
    bodies.push(JSON.parse(init.body as string));
    return jsonResponse(200, liveRecallBody([{ id: "m1", content: "a" }]));
  });

  const result = await client.recall("q");
  await client.recall("q", { items_alias: true });

  assert.equal(bodies[0].items_alias, false);
  assert.equal(result.supportingMemories[0].id, "m1");
  assert.equal(bodies[1].items_alias, true);
});

test("L-94: search keeps the envelope around its results", async () => {
  const envelope = {
    items: [{ id: "m1", content: "a" }],
    recall_tracked: true,
    diagnostic: { candidates: 3 },
    warnings: [
      {
        code: "unrecognized_parameters",
        message: "bogus is not a /search parameter",
        details: { params: ["bogus"] },
      },
    ],
  };
  const client = makeClient(() => jsonResponse(200, envelope));

  const results = await client.search("q", { diagnostic: true, bogus: 1 });

  assert.ok(Array.isArray(results));
  assert.equal(results.length, 1);
  assert.equal(results[0].id, "m1");
  const kept = results as unknown as Record<string, unknown>;
  assert.equal(kept.recallTracked, true);
  assert.deepEqual(kept.diagnostic, { candidates: 3 });
  assert.deepEqual(kept.warnings, envelope.warnings);
  assert.deepEqual(kept.raw, envelope);
});

test("L-94: an envelope without the optional fields reads as null", async () => {
  const client = makeClient(() => jsonResponse(200, { items: [] }));

  const results = await client.search("q");

  assert.equal(results.length, 0);
  const kept = results as unknown as Record<string, unknown>;
  assert.equal(kept.recallTracked, null);
  assert.equal(kept.diagnostic, null);
  assert.equal(kept.warnings, null);
});

for (const method of ["search", "recall"] as const) {
  test(`L-03: ${method} sends callerAgentId only when asked`, async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const client = makeClient(
      (_url, init) => {
        bodies.push(JSON.parse(init.body as string));
        return jsonResponse(200, method === "search" ? { items: [] } : liveRecallBody([]));
      },
      { agentId: "a1" },
    );

    await client[method]("q");
    await client[method]("q", { callerAgentId: "a1" });

    // Unset, the client's agentId is not asserted: the server would narrow the
    // read to that agent's fleet and trust (Eldad, 2026-10-08).
    assert.equal("caller_agent_id" in bodies[0], false);
    assert.equal(bodies[1].caller_agent_id, "a1");
    assert.equal("callerAgentId" in bodies[1], false);
  });
}
