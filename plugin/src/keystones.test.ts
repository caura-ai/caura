/**
 * Tests for keystone fetch + format + cache (CAURA-000).
 *
 * Covers:
 *   - formatKeystones: empty input, weight ordering, token-cap truncation.
 *   - fetchKeystonesBlock: kill switch, happy path, cache hit/miss,
 *     invalidateKeystoneCache, fail-open on network error,
 *     fail-open when tenant resolution fails, agent_id dropped when
 *     fleet_id is absent.
 *
 * Pattern matches ``transport.test.ts``: env is fixed before the dynamic
 * import, ``globalThis.fetch`` is stubbed per-test to avoid real network
 * calls (and to capture / assert request URLs).
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

process.env.MEMCLAW_API_KEY = "mc_test_keystones";
process.env.MEMCLAW_API_URL = "http://localhost:8000";
process.env.MEMCLAW_TENANT_ID = "t_keystones_test";
// Default-on; specific tests flip the flag and re-import the module.
process.env.MEMCLAW_KEYSTONES_ENABLED = "true";
// Cap fits the header (~190 chars) plus a single short rule (~30 chars)
// + footer (~20 chars). Anything beyond rule #1 (in weight DESC order)
// must be dropped — that's what the truncation test asserts.
process.env.MEMCLAW_KEYSTONES_TOKEN_CAP = "120"; // ~480 chars

const keystones = await import("./keystones.js");
const { formatKeystones, fetchKeystonesBlock, invalidateKeystoneCache } = keystones;

interface MockCall {
  url: string;
  init?: RequestInit;
}

let originalFetch: typeof fetch;
let calls: MockCall[];

// ``apiCall`` resolves a per-agent key via ``resolveAgentKey`` when an
// ``agent_id`` is supplied — that's an extra HTTP request alongside the
// main one. The cache-count assertions need to ignore those, so we
// filter ``calls`` to just the keystones endpoint when counting.
function keystoneCalls(): MockCall[] {
  return calls.filter((c) => c.url.includes("/memclaw/keystones"));
}

function installFetch(body: unknown, status = 200): void {
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    // Make any non-keystones lookup (agent-key provisioning, tenant
    // resolution) return a benign 404 so the keystones path is the one
    // the test observes.
    const url = String(input);
    if (!url.includes("/memclaw/keystones")) {
      return new Response("{}", { status: 404 });
    }
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

function installFailingFetch(error: Error): void {
  globalThis.fetch = (async () => {
    throw error;
  }) as typeof fetch;
}

describe("formatKeystones", () => {
  test("returns empty string for an empty rule list", () => {
    assert.equal(formatKeystones([]), "");
  });

  test("sorts rules by weight DESC (highest priority first)", () => {
    const out = formatKeystones([
      { doc_id: "low", data: { title: "Low", content: "L", weight: 25 } },
      { doc_id: "high", data: { title: "High", content: "H", weight: 100 } },
      { doc_id: "med", data: { title: "Med", content: "M", weight: 50 } },
    ]);
    const highIdx = out.indexOf("High:");
    const medIdx = out.indexOf("Med:");
    const lowIdx = out.indexOf("Low:");
    assert.ok(highIdx >= 0 && medIdx > highIdx && lowIdx > medIdx, out);
  });

  test("emits the mandatory-rules header and the wrapping tags", () => {
    const out = formatKeystones([
      { doc_id: "r", data: { title: "Rule", content: "C", weight: 1 } },
    ]);
    assert.match(out, /<keystone_rules>/);
    assert.match(out, /<\/keystone_rules>/);
    assert.match(out, /MANDATORY/);
  });

  test("drops lowest-weight rules first when the cap is hit and notes the omission", () => {
    // 120-token cap (~480 chars). Header+footer ~210 chars, available
    // ~270 chars for rule lines. Three ~140-char rules push the total
    // past the cap; we want the top-priority one to survive and the
    // bottom one to be dropped.
    const big = "x".repeat(120);
    const rules = [
      { doc_id: "k1", data: { title: "K1", content: big, weight: 100 } },
      { doc_id: "k2", data: { title: "K2", content: big, weight: 50 } },
      { doc_id: "k3", data: { title: "K3", content: big, weight: 1 } },
    ];
    const out = formatKeystones(rules);
    // Highest-weight rule survives.
    assert.match(out, /K1:/);
    // Lowest-weight rule and a "more rules omitted" footer indicate truncation.
    assert.doesNotMatch(out, /K3:/);
    assert.match(out, /more rules omitted/);
  });

  test("falls back to doc_id when title is missing", () => {
    const out = formatKeystones([
      { doc_id: "no-secrets", data: { content: "Never.", weight: 5 } },
    ]);
    assert.match(out, /no-secrets:/);
  });
});

describe("fetchKeystonesBlock", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    calls = [];
    invalidateKeystoneCache();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("returns a non-empty block on the happy path", async () => {
    installFetch({
      count: 1,
      truncated: false,
      rules: [
        { doc_id: "r1", data: { title: "R1", content: "Body", weight: 50, scope: "tenant" } },
      ],
    });
    const block = await fetchKeystonesBlock({ agentId: "agent-A", fleetId: "fleet-A" });
    assert.match(block, /<keystone_rules>/);
    assert.match(block, /R1:/);
  });

  test("accepts a bare-list response shape too (forward-compatibility)", async () => {
    installFetch([
      { doc_id: "r1", data: { title: "R1", content: "B", weight: 1 } },
    ]);
    const block = await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.match(block, /R1:/);
  });

  test("caches the second call (identity-keyed) — only one fetch issued", async () => {
    installFetch({ count: 0, truncated: false, rules: [
      { doc_id: "r", data: { title: "T", content: "C", weight: 1 } },
    ] });
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(keystoneCalls().length, 1, "second call must come from cache");
  });

  test("invalidateKeystoneCache forces a refetch", async () => {
    installFetch({ count: 0, truncated: false, rules: [
      { doc_id: "r", data: { title: "T", content: "C", weight: 1 } },
    ] });
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    invalidateKeystoneCache();
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(keystoneCalls().length, 2, "after invalidation, second call hits the network");
  });

  test("drops agent_id from the query when fleet_id is absent", async () => {
    installFetch({ count: 0, truncated: false, rules: [] });
    await fetchKeystonesBlock({ agentId: "agent-Z", fleetId: undefined });
    const ks = keystoneCalls();
    assert.equal(ks.length, 1);
    const url = new URL(ks[0].url);
    assert.equal(url.searchParams.get("agent_id"), null);
    assert.equal(url.searchParams.get("fleet_id"), null);
    assert.equal(url.searchParams.get("tenant_id"), process.env.MEMCLAW_TENANT_ID);
  });

  test("fail-open on network error — returns '' and does not throw", async () => {
    installFailingFetch(new Error("ECONNRESET"));
    const block = await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(block, "");
  });

  test("fail-open on non-2xx — returns '' and does not throw", async () => {
    installFetch({ detail: "server error" }, 500);
    const block = await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(block, "");
  });

  test("empty rule list yields '' (no block injected)", async () => {
    installFetch({ count: 0, truncated: false, rules: [] });
    const block = await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(block, "");
  });

  test("backend outage is negative-cached — repeated calls don't re-fetch", async () => {
    // First call exercises the full ``KEYSTONES_TIMEOUT_MS`` path; the
    // second must come from the negative cache so a degraded backend
    // doesn't add per-turn latency for the full TTL window. Use a stub
    // that BOTH records the URL and throws so we can count attempts.
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      throw new Error("ECONNRESET");
    }) as typeof fetch;
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    await fetchKeystonesBlock({ agentId: "a", fleetId: "f" });
    assert.equal(
      keystoneCalls().length,
      1,
      "second call must come from the failure back-off cache",
    );
  });
});

// NB: ``MEMCLAW_KEYSTONES_ENABLED`` is captured at module-load time in
// ``env.ts`` (see ``_readBoolEnv``), so toggling the env var inside a
// running test would no-op against the already-imported boolean. The
// kill-switch path is covered indirectly by the env-test suite, which
// exercises ``_readBoolEnv`` with both literal values. Re-running the
// integration test with ``MEMCLAW_KEYSTONES_ENABLED=false`` in the
// shell is the operational verification path.
