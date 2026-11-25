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
import { createServer } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "mc_test_key_for_transport_tests";
process.env.CAURA_API_URL = "http://localhost:8000";
process.env.CAURA_TENANT_ID = "t_test";
const originalHome = process.env.HOME;
const tmpHome = mkdtempSync(join(tmpdir(), "transport-test-home-"));
process.env.HOME = tmpHome;
mkdirSync(join(tmpHome, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

const { apiCall: request, parseSearchItems } = await import("./transport.js");
const apiCall: typeof request = (...args) => {args[5] ??= "fixture-agent"; return request(...args);};
const { USER_AGENT } = await import("./user-agent.js");
const { isNetworkClassError } = await import("./health.js");

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

  test("exposes response headers without changing an array payload", async () => {
    globalThis.fetch = (async () => new Response(JSON.stringify([{ id: "r1" }]), {
      status: 200, headers: { "X-Truncated": "true" },
    })) as typeof fetch;
    let truncated: string | null = null;
    const result = await apiCall("GET", "/memories", undefined, undefined, undefined,
      undefined, undefined, (headers) => { truncated = headers.get("X-Truncated"); });
    assert.equal(truncated, "true");
    assert.deepEqual(result, [{ id: "r1" }]);
  });

  for (const status of [500, 502, 503]) test(`HTTP ${status} reports a server error without echoing its body`, async () => {
    globalThis.fetch = async () => new Response('backend detail mc_fixture_secret https://user:pass@example.test', {status});
    await assert.rejects(apiCall("GET", "/memories"), (e: Error) => {
      assert.equal(e.message, `Caura API ${status}: server error`); assert.equal(isNetworkClassError(e), true); return true;
    });
  });
  test("does not publish success metadata for failed responses", async () => {
    globalThis.fetch = (async () => new Response("unavailable", {
      status: 503, headers: { "X-Truncated": "true" },
    })) as typeof fetch;
    let notified = false;
    await assert.rejects(apiCall("GET", "/memories", undefined, undefined, undefined,
      undefined, undefined, () => { notified = true; }), /Caura API 503/);
    assert.equal(notified, false);
  });

  test("422 preserves bounded validation detail and redacts credentials", async () => {
    const body = String.raw`invalid field mc_echoed_key mca_echoed_key mci_echoed_key mcx_echoed_key mco_echoed_key caura_echoed_key sk-echoed-key https://user:pass@example.test/path https:\/\/other:pass@example.test/path ` + 'x'.repeat(300);
    globalThis.fetch = async () => new Response(body, {status: 422});
    await assert.rejects(apiCall("POST", "/memories", {}), (e: Error) => {
      assert.match(e.message, /invalid field/); assert.doesNotMatch(e.message, /mc[a-z]?_echoed|caura_echoed|sk-echoed|user:pass|example.test/);
      assert.ok(e.message.length <= "Caura API 422: ".length + 200); return true;
    });
  });
  for (const body of ['Field required in header', 'authorization field is required', 'cookie field is missing', 'headers were invalid',
    JSON.stringify({detail: [{loc: ["header", "x-api-key"], msg: "Field required"}]})]) test(`422 keeps benign validation detail: ${body}`, async () => {
    globalThis.fetch = async () => new Response(body, {status: 422});
    await assert.rejects(apiCall("POST", "/memories", {}), (e: Error) => {assert.equal(e.message, `Caura API 422: ${body}`); return true;});
  });
  test("4xx request-header echoes are never included in diagnostics", async () => {
    for (const body of [
      '{"headers":{"X-API-Key":"fixture_sensitive"}}',
      'X-API-Key fixture_sensitive',
      'key=fixture_sensitive',
      '["X-API-Key","fixture_sensitive"]',
      '{"Authorization":"Bearer fixture_sensitive"}',
      JSON.stringify({detail: JSON.stringify({headers: {"X-API-Key": "fixture_sensitive"}})})
    ]) {
      globalThis.fetch = async () => new Response(body, {status: 409});
      await assert.rejects(apiCall("POST", "/memories", {}), /^Error: Caura API 409: request refused$/);
    }
  });
  for (const status of [401, 403]) test(`HTTP ${status} drops response bodies and header echoes`, async () => {
    globalThis.fetch = async () => new Response('X-API-Key: fixture_sensitive echo', {status});
    await assert.rejects(apiCall("POST", "/memories", {}), new RegExp(`^Error: Caura API ${status}: request refused$`));
    globalThis.fetch = async () => Response.json({detail: "agent_key_revoked", echoed: "fixture_sensitive"}, {status});
    await assert.rejects(apiCall("POST", "/memories", {}), new RegExp(`^Error: Caura API ${status}: agent_key_revoked$`));
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

  test("sends X-API-Key over plain HTTP to loopback (no opt-in needed)", async () => {
    await apiCall("GET", "/memories");
    const headers = calls[0].init?.headers as Record<string, string>;
    assert.equal(headers["X-API-Key"], "mc_test_key_for_transport_tests");
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

  test("L-58: an undefined or null query value is left out, not sent as text", async () => {
    // Callers cast optional fields into the string map; URLSearchParams.set
    // stringifies them, so the server read ``collection=undefined``.
    await apiCall("GET", "/memories", undefined, {
      tenant_id: "t1",
      collection: undefined as unknown as string,
      fleet_id: null as unknown as string,
    });
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

test("real fetch never sends the API key to a redirect destination", async () => {
  const realFetch = globalThis.fetch;
  let sourceHits = 0;
  let destinationHits = 0;
  let redirectStatus = 302;
  const destination = createServer((_req, res) => {
    destinationHits++;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true }));
  });
  destination.listen(0, "127.0.0.1");
  await once(destination, "listening");
  const target = `http://127.0.0.1:${(destination.address() as AddressInfo).port}/target`;
  const source = createServer((req, res) => {
    sourceHits++;
    assert.equal(req.headers["x-api-key"], process.env.CAURA_API_KEY);
    res.writeHead(redirectStatus, { Location: target });
    res.end();
  });
  try {
    source.listen(0, "127.0.0.1");
    await once(source, "listening");
    const origin = `http://127.0.0.1:${(source.address() as AddressInfo).port}`;
    // Route only the configured API request to our ephemeral source server.
    // Redirect handling remains the real fetch implementation's responsibility.
    globalThis.fetch = (input, init) => realFetch(new URL(new URL(String(input)).pathname, origin), init);
    for (const status of [301, 302, 303, 307, 308]) {
      redirectStatus = status;
      await assert.rejects(() => apiCall("GET", "/memories"), TypeError);
    }
    assert.equal(sourceHits, 5);
    assert.equal(destinationHits, 0, "no request or credential may reach the redirected host");
  } finally {
    globalThis.fetch = realFetch;
    for (const server of [source, destination]) {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve()));
    }
  }
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
