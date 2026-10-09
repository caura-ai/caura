/**
 * L-163 / L-199: what ``caura_recall`` puts in the agent's context.
 *
 * The plugin returned ``{ results }`` where ``results`` was the whole
 * ``POST /search`` response, an object where MCP returns a list. With
 * ``include_brief`` it then called ``POST /recall``, which runs the same search
 * again, and returned that whole body as ``brief``: one call put every row into
 * the context three times (``results.items``, ``brief.memories`` and
 * ``brief.items``) and paid for two searches.
 *
 * Decided 2026-10-09 (Eldad): the rows once. ``results`` is the row list, with
 * ``count``. ``include_brief`` makes one ``/recall`` call that asks for no
 * ``items`` copy; its rows become ``results`` and ``brief`` keeps the summary.
 */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";

// Read into module constants at import time, so it must precede the import.
process.env.CAURA_API_URL = "https://recall-envelope.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-recall-envelope";

const { createToolFromSpec } = await import("./tool-definitions.js");

const ROWS = [
  { id: "11111111-1111-4111-8111-111111111111", content: "deploy with the blue-green runbook" },
  { id: "22222222-2222-4222-8222-222222222222", content: "roll back by repointing the alias" },
];
const SUMMARY = "Deploy blue-green; roll back by repointing the alias.";
// A query the mock answers with an empty (null) /recall body.
const NOTHING_RECALLED = "a query the server answers with no body";

interface Captured {
  path: string;
  body: Record<string, unknown> | undefined;
}

let captured: Captured[] = [];
let realFetch: typeof globalThis.fetch;

before(() => {
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    captured.push({ path, body });
    // The two REST shapes, as core-api serialises them.
    let payload: unknown = { ok: true };
    if (path.endsWith("/search")) {
      payload = { items: ROWS, recall_tracked: true, diagnostic: null, warnings: null };
    } else if (path.endsWith("/recall") && body?.query === NOTHING_RECALLED) {
      payload = null;
    } else if (path.endsWith("/recall")) {
      payload = {
        query: body?.query,
        summary: SUMMARY,
        memory_count: ROWS.length,
        memories: ROWS,
        ...(body?.items_alias === false ? {} : { items: ROWS }),
        recall_ms: 812,
      };
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

/** The tool's result as the agent reads it. */
async function recall(params: Record<string, unknown>): Promise<Record<string, any>> {
  captured = [];
  const out = await createToolFromSpec("caura_recall").execute(
    "test-call",
    params,
    undefined as never,
  );
  return JSON.parse(out.content[0].text);
}

/** How many times one row reaches the agent's context. */
function copiesOfARow(result: Record<string, unknown>): number {
  return JSON.stringify(result).split(ROWS[0].content).length - 1;
}

test("L-163: results is the row list, with its count, and each row appears once", async () => {
  const result = await recall({ query: "how do we deploy" });

  assert.ok(Array.isArray(result.results), "results is not a list");
  assert.deepEqual(
    result.results.map((r: { id: string }) => r.id),
    ROWS.map((r) => r.id),
  );
  assert.equal(result.count, ROWS.length);
  assert.equal(result.recall_tracked, true);
  assert.equal(copiesOfARow(result), 1);
  // Nothing to report: the server's null warnings and diagnostic are left out.
  assert.equal("warnings" in result, false);
  assert.equal("diagnostic" in result, false);
});

test("L-199: include_brief makes one /recall call, and each row appears once", async () => {
  const result = await recall({ query: "how do we deploy", include_brief: true });

  assert.deepEqual(
    captured.map((c) => c.path),
    ["/api/v1/recall"],
    "a brief needs one search, not a /search and then a /recall",
  );
  assert.equal(captured[0].body?.items_alias, false);
  assert.ok(Array.isArray(result.results), "results is not a list");
  assert.equal(result.count, ROWS.length);
  assert.equal(result.brief?.summary, SUMMARY);
  assert.equal(copiesOfARow(result), 1);
});

test("an empty /recall body gives an empty result, not a TypeError", async () => {
  const result = await recall({ query: NOTHING_RECALLED, include_brief: true });

  assert.deepEqual(result.results, []);
  assert.equal(result.count, 0);
});
