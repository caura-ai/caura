/**
 * Tests for apiCall in transport.ts.
 *
 * Guards the API-prefix consolidation: all resource paths must be
 * auto-prepended with CAURA_API_PREFIX, and absolute "/api/..." paths
 * must be rejected so regressions surface at test time.
 */
import { test, describe, beforeEach, afterEach, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "mc_test_key_for_transport_tests";
process.env.CAURA_API_URL = "http://localhost:8000";
process.env.CAURA_TENANT_ID = "t_test";
const originalHome = process.env.HOME;
const tmpHome = mkdtempSync(join(tmpdir(), "transport-test-home-"));
process.env.HOME = tmpHome;
mkdirSync(join(tmpHome, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

const { apiCall, parseSearchItems } = await import("./transport.js");
const { USER_AGENT } = await import("./user-agent.js");

interface MockCall {
  url: string;
  init?: RequestInit;
}

let originalFetch: typeof fetch;
let calls: MockCall[];

after(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(tmpHome, { recursive: true, force: true });
});

function installOkFetch(): void {
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

describe("apiCall — CAURA_API_PREFIX handling", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    installOkFetch();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("prepends CAURA_API_PREFIX to resource paths", async () => {
    await apiCall("POST", "/search", { q: "x" });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "http://localhost:8000/api/v1/search");
  });

  test("rejects paths starting with CAURA_API_PREFIX", async () => {
    await assert.rejects(
      () => apiCall("POST", "/api/v1/search", {}),
      /apiCall path must be a resource path/,
    );
    assert.equal(calls.length, 0, "should not reach fetch");
  });

  test("rejects paths with prefix but no leading slash", async () => {
    await assert.rejects(
      () => apiCall("POST", "api/v1/search", {}),
      /apiCall path must be a resource path/,
    );
    assert.equal(calls.length, 0, "should not reach fetch");
  });

  test("normalizes missing leading slash", async () => {
    await apiCall("GET", "memories");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "http://localhost:8000/api/v1/memories");
  });

  test("query params survive prefix prepend", async () => {
    await apiCall("GET", "/memories", undefined, { tenant_id: "t1" });
    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      "http://localhost:8000/api/v1/memories?tenant_id=t1",
    );
  });
});

describe("apiCall — extraHeaders (bulk X-Bulk-Attempt-Id support)", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    installOkFetch();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("merges extraHeaders into the request headers", async () => {
    // No agent_id in the body: that would trigger resolveAgentKey() and add
    // a second fetch, which isn't what this test is asserting.
    await apiCall(
      "POST",
      "/memories/bulk",
      { items: [{ content: "x" }], tenant_id: "t1" },
      undefined,
      undefined,
      undefined,
      { "X-Bulk-Attempt-Id": "attempt-123" },
    );
    assert.equal(calls.length, 1);
    const headers = calls[0].init?.headers as Record<string, string>;
    assert.equal(headers["X-Bulk-Attempt-Id"], "attempt-123");
    // Default headers must survive alongside the caller-supplied ones.
    // (Assert presence, not the exact key value, which env.ts may source
    // from a local .env rather than the test's process.env.)
    assert.ok(headers["X-API-Key"], "X-API-Key should still be sent");
    assert.equal(headers["Content-Type"], "application/json");
    assert.equal(headers["User-Agent"], USER_AGENT, "User-Agent should still be sent");
  });

  test("omitting extraHeaders leaves only the default headers", async () => {
    await apiCall("POST", "/memories", { content: "x" });
    const headers = calls[0].init?.headers as Record<string, string>;
    assert.equal(headers["X-Bulk-Attempt-Id"], undefined);
    assert.ok(headers["X-API-Key"], "X-API-Key should still be sent");
    assert.equal(headers["User-Agent"], USER_AGENT);
  });
});

describe("apiCall — rejected agent key fallback", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("bounds fallback, preserves idempotency, and re-provisions on the next call", async () => {
    let provisions = 0;
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.endsWith("/admin/agent-keys/provision")) {
        provisions++;
        return new Response(
          JSON.stringify({ raw_key: `agent-key-${provisions}`, key_prefix: "agent" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      const key = (init?.headers as Record<string, string>)["X-API-Key"];
      if (key === "agent-key-1") return new Response("rejected", { status: 401 });
      if (key === "agent-key-2") {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      assert.equal(key, process.env.CAURA_API_KEY);
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;

    const body = {
      agent_id: "bounded-retry-agent",
      items: [{ content: "remember this" }],
    };
    const extraHeaders = { "X-Bulk-Attempt-Id": "attempt-fixed" };
    const result = await apiCall(
      "POST",
      "/memories/bulk",
      body,
      undefined,
      undefined,
      undefined,
      extraHeaders,
    );
    await apiCall(
      "POST",
      "/memories/bulk",
      body,
      undefined,
      undefined,
      undefined,
      extraHeaders,
    );

    assert.equal((result as { ok: boolean }).ok, true);
    assert.equal(typeof (result as { _latency_ms: unknown })._latency_ms, "number");
    assert.equal(provisions, 2, "the later top-level call must replace the evicted key");
    const resourceCalls = calls.filter((call) => call.url.endsWith("/memories/bulk"));
    assert.deepEqual(
      resourceCalls.map(
        (call) => (call.init?.headers as Record<string, string>)["X-API-Key"],
      ),
      ["agent-key-1", process.env.CAURA_API_KEY, "agent-key-2"],
    );
    assert.deepEqual(
      resourceCalls.map(
        (call) => (call.init?.headers as Record<string, string>)["X-Bulk-Attempt-Id"],
      ),
      ["attempt-fixed", "attempt-fixed", "attempt-fixed"],
    );
  });
});

describe("apiCall — User-Agent identification (Caura Heartbeat v1 §7)", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    installOkFetch();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("every request carries the openclaw-plugin User-Agent", async () => {
    await apiCall("GET", "/memories");
    await apiCall("POST", "/search", { q: "x" });
    assert.equal(calls.length, 2);
    for (const call of calls) {
      const headers = call.init?.headers as Record<string, string>;
      assert.equal(headers["User-Agent"], USER_AGENT, `missing on ${call.url}`);
      assert.match(headers["User-Agent"], /^openclaw-plugin\//);
    }
  });
});

describe("parseSearchItems — search-response shape handling", () => {
  // Regression guard: the REST /search endpoint returns { items: [...] }
  // (core-api SearchResponse), but the plugin historically read .results
  // and silently got [] — breaking context-engine auto-recall and the
  // bootstrap smoke test. items must win, with results + bare-array
  // fallbacks for the MCP-shaped and legacy responses.
  const m = (id: string) => ({ id, content: `c-${id}` });

  test("reads the REST `items` array", () => {
    const out = parseSearchItems({ items: [m("a"), m("b")] });
    assert.deepEqual(out.map((r) => r.id), ["a", "b"]);
  });

  test("falls back to `results` (MCP-shaped response)", () => {
    const out = parseSearchItems({ results: [m("a")] });
    assert.deepEqual(out.map((r) => r.id), ["a"]);
  });

  test("prefers `items` over `results` when both present", () => {
    const out = parseSearchItems({ items: [m("i")], results: [m("r")] });
    assert.deepEqual(out.map((r) => r.id), ["i"]);
  });

  test("accepts a bare array", () => {
    const out = parseSearchItems([m("a"), m("b")]);
    assert.equal(out.length, 2);
  });

  test("returns [] for empty / null / primitive / missing keys", () => {
    assert.deepEqual(parseSearchItems({}), []);
    assert.deepEqual(parseSearchItems(null), []);
    assert.deepEqual(parseSearchItems(undefined), []);
    assert.deepEqual(parseSearchItems("nope"), []);
    assert.deepEqual(parseSearchItems({ items: undefined }), []);
  });

  test("returns [] when items/results is present but NOT an array (never throws)", () => {
    // Malformed server responses must not slip through the cast and make
    // callers' .map/.length throw.
    assert.deepEqual(parseSearchItems({ items: {} }), []);
    assert.deepEqual(parseSearchItems({ items: "oops" }), []);
    assert.deepEqual(parseSearchItems({ items: 42 }), []);
    assert.deepEqual(parseSearchItems({ results: { nested: true } }), []);
  });
});
