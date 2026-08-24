/**
 * The configured-fleet default is a DEFAULT, not an override.
 *
 * ``enrichBody`` fills in ``fleet_id`` from ``CAURA_FLEET_ID`` whenever the
 * caller omits it, so ordinary calls are fleet-scoped without every agent
 * having to say so. But ``scope: 'all'`` is a caller explicitly asking to span
 * every fleet, and on the two read-enumeration surfaces the server applies
 * ``fleet_id`` as an unconditional row filter — ``memory_list_by_filters`` and
 * ``memory_stats_breakdown`` both do ``if fleet_id: Memory.fleet_id ==
 * fleet_id`` OUTSIDE any scope branch, and ``resolve_read_fleet_gate`` passes
 * ``fleet_id`` straight through for ``scope='all'``. Defaulting it in there
 * turns a tenant-wide read into a single-fleet one, and because the filter is a
 * strict equality it also drops every fleet-less (``fleet_id IS NULL``) row.
 * Nothing in the response says the result was narrowed.
 *
 * These tests assert on the QUERY STRING the plugin actually puts on the wire.
 * Asserting that the tool "returns results" would pass either way — the
 * narrowed call succeeds, it just answers with less.
 *
 * The env vars are read at ``env.ts`` import time, so they are set before the
 * dynamic import below (same pattern as ``env.test.ts``).
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

const CONFIGURED_FLEET = "fleet-from-env";

// A pinned tenant keeps ``ensureTenantId`` from reaching for the network.
process.env.CAURA_TENANT_ID = "tenant-fleet-scope-test";
process.env.CAURA_FLEET_ID = CONFIGURED_FLEET;
delete process.env.CAURA_API_KEY;

const { createToolFromSpec } = await import("./tool-definitions.js");

/** The endpoint each tool under test dispatches to. */
const ENDPOINTS: Record<string, string> = {
  caura_list: "/memories",
  caura_stats: "/memories/stats",
  caura_insights: "/insights/generate",
  caura_evolve: "/evolve/report",
};

let originalFetch: typeof fetch;

/**
 * Run one tool call against a stubbed transport and return the request it made
 * to the tool's own endpoint.
 *
 * Matching on the endpoint rather than taking the only request keeps the test
 * hermetic against an ambient API key, which would switch on ``agent-auth``
 * and add a provisioning call. Asserting that exactly one MATCHING request
 * happened is what stops an assertion from passing over a request that was
 * never made.
 */
async function callAndCaptureRequest(
  toolName: string,
  params: Record<string, unknown>,
): Promise<{ query: URLSearchParams; body: Record<string, unknown> }> {
  const matched: { url: URL; rawBody?: string }[] = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith(ENDPOINTS[toolName])) {
      matched.push({ url, rawBody: init?.body as string | undefined });
    }
    return new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  await createToolFromSpec(toolName).execute("test-call", params);

  assert.equal(
    matched.length,
    1,
    `${toolName} should hit ${ENDPOINTS[toolName]} exactly once, hit it ${matched.length}x`,
  );
  const { url, rawBody } = matched[0];
  return {
    query: url.searchParams,
    body: rawBody ? JSON.parse(rawBody) : {},
  };
}

describe("configured-fleet default vs an explicit scope", () => {
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  // THE regression. Without the fix this request carries
  // ``fleet_id=fleet-from-env`` and the tenant-wide read silently comes back
  // narrowed to one fleet.
  test("caura_list scope='all' does not inherit the configured fleet", async () => {
    const { query } = await callAndCaptureRequest("caura_list", { scope: "all" });
    assert.equal(query.get("scope"), "all");
    assert.equal(
      query.get("fleet_id"),
      null,
      "scope='all' asked to span fleets; a fleet_id here narrows it back to one",
    );
  });

  // Same defect, same server code path (``_resolve_scoped_read`` is shared by
  // GET /memories and GET /memories/stats), so a count can't disagree with the
  // listing it summarises.
  test("caura_stats scope='all' does not inherit the configured fleet", async () => {
    const { query } = await callAndCaptureRequest("caura_stats", { scope: "all" });
    assert.equal(query.get("scope"), "all");
    assert.equal(query.get("fleet_id"), null);
  });

  // The other half of the contract: the default must SURVIVE for every caller
  // that did not ask to span fleets. Omitting ``scope`` is not the same request
  // as ``scope: 'all'`` — this is the distinction the fix turns on.
  test("caura_list keeps the configured fleet when scope is omitted", async () => {
    const { query } = await callAndCaptureRequest("caura_list", {});
    assert.equal(query.get("scope"), null);
    assert.equal(query.get("fleet_id"), CONFIGURED_FLEET);
  });

  test("caura_list keeps the configured fleet for scope='agent' and 'fleet'", async () => {
    for (const scope of ["agent", "fleet"]) {
      const { query } = await callAndCaptureRequest("caura_list", { scope });
      assert.equal(query.get("scope"), scope);
      assert.equal(
        query.get("fleet_id"),
        CONFIGURED_FLEET,
        `scope='${scope}' is a fleet-scoped read; the default belongs here`,
      );
    }
  });

  test("caura_stats keeps the configured fleet when scope is omitted", async () => {
    const { query } = await callAndCaptureRequest("caura_stats", {});
    assert.equal(query.get("fleet_id"), CONFIGURED_FLEET);
  });

  // A caller-supplied fleet_id is never touched: the fix withholds a default,
  // it does not strip a value. ``scope='all'`` with an explicit fleet is a
  // legitimate cross-fleet read of ONE named fleet.
  test("an explicit fleet_id survives scope='all'", async () => {
    const { query } = await callAndCaptureRequest("caura_list", {
      scope: "all",
      fleet_id: "fleet-named-by-caller",
    });
    assert.equal(query.get("fleet_id"), "fleet-named-by-caller");
  });

  // ``caura_insights`` and ``caura_evolve`` also take scope='all', and they are
  // deliberately NOT part of the fix: their ``fleet_id`` is not merely a read
  // filter. Server-side both branch on ``scope`` and ignore ``fleet_id``
  // entirely under 'all' (``_insights_scope_filters`` /
  // ``evolve_service._filter_by_scope``), so there is no read to widen — while
  // both PERSIST with it (``BulkMemoryCreate(fleet_id=...)`` for insight
  // findings, ``_persist_rule`` for evolve outcomes, plus insights'
  // ``supersede_priors`` key). Withholding it there would relocate a write and
  // strand the priors it was supposed to supersede.
  test("caura_insights and caura_evolve keep the configured fleet at scope='all'", async () => {
    const insights = await callAndCaptureRequest("caura_insights", {
      focus: "patterns",
      scope: "all",
    });
    assert.equal(insights.body.fleet_id, CONFIGURED_FLEET);

    const evolve = await callAndCaptureRequest("caura_evolve", {
      outcome: "test outcome",
      outcome_type: "success",
      scope: "all",
    });
    assert.equal(evolve.body.fleet_id, CONFIGURED_FLEET);
  });
});
