/**
 * An op-dispatched tool must not treat an unrecognised op as its last branch.
 *
 * `caura_manage` and `caura_doc` each ended their `if (op === …)` chain in a
 * comment rather than a condition — `// op === "update"` and
 * `// op === "delete"` — so the final branch doubled as the else. Every value
 * the chain did not match became a WRITE. Measured against that code:
 *
 *     caura_manage op="lineage"  ->  PATCH  /api/v1/memories/{id}
 *     caura_doc    op="remove"   ->  DELETE /api/v1/documents/doc-1
 *
 * `lineage` is a READ. `bulk_delete` and `lineage` are realistic inputs, not
 * invented ones: the MCP handler accepts both, the tool description shared by
 * both surfaces names both, and this plugin has an endpoint for neither. A
 * near-miss typo lands in the same place.
 *
 * The only thing that had kept those branches unreachable was the `op` enum in
 * `PARAM_SCHEMAS`, enforced by the host's schema validator rather than by this
 * package.
 *
 * These assert on the requests the server would actually receive, and assert
 * that BEFORE asserting the refusal. Ordering is load-bearing twice over: a
 * test that only checked for a throw would pass on a dispatcher that mutated
 * and then threw, and `assert.rejects` first would abort before the mutation
 * check and report "missing expected rejection" instead of naming the DELETE.
 */
import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";

// Read into module constants at import time, so it must precede the import.
process.env.CAURA_API_URL = "http://op-dispatch.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-op-dispatch";

const { createToolFromSpec } = await import("./tool-definitions.js");

interface Captured {
  url: URL;
  method: string;
}

let captured: Captured[] = [];
let realFetch: typeof globalThis.fetch;

before(() => {
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured.push({ url: new URL(String(input)), method: init?.method ?? "GET" });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
});

after(() => {
  globalThis.fetch = realFetch;
});

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Mutating requests against the resource routes.
 *
 * Filtered by path, not method alone: `apiCall` provisions an agent-scoped
 * credential with its own POST whenever a call carries an `agent_id`, and
 * counting that would make a passing dispatch look like a stray write.
 */
function mutations(): string[] {
  return captured
    .filter(
      (c) =>
        MUTATING.has(c.method) &&
        (c.url.pathname.includes("/memories") || c.url.pathname.includes("/documents")),
    )
    .map((c) => `${c.method} ${c.url.pathname}`);
}

/** Resolves to the thrown error on refusal, or the result if it was accepted. */
async function outcomeOf(
  tool: string,
  params: Record<string, unknown>,
): Promise<unknown> {
  captured = [];
  return createToolFromSpec(tool)
    .execute("test-call", params, undefined as never)
    .catch((e: unknown) => e);
}

const MEMORY_ID = "11111111-1111-4111-8111-111111111111";

describe("an unrecognised op is refused, not dispatched as a write", () => {
  // One value per tool: after the guard there is no branching on the op VALUE,
  // so every unmatched string reaches the same line and further values would
  // re-test one path. `bulk_delete` is the realistic case for caura_manage —
  // the server accepts it and MCP_ONLY_OPS in tool-definitions.test.ts records
  // that this surface does not. caura_doc has no MCP-only op, so a near-miss
  // typo stands in.
  test("caura_manage op=bulk_delete does not become a PATCH", async () => {
    const outcome = await outcomeOf("caura_manage", {
      op: "bulk_delete",
      memory_id: MEMORY_ID,
      content: "x",
    });
    assert.deepEqual(mutations(), [], "reached a mutating request");
    assert.match(String(outcome), /unsupported op/, "was accepted, not refused");
  });

  test("caura_doc op=remove does not become a DELETE", async () => {
    const outcome = await outcomeOf("caura_doc", {
      op: "remove",
      doc_id: "doc-1",
      collection: "c-1",
    });
    assert.deepEqual(mutations(), [], "reached a mutating request");
    assert.match(String(outcome), /unsupported op/, "was accepted, not refused");
  });

  test("the refusal names the ops this surface does offer", async () => {
    const outcome = await outcomeOf("caura_manage", {
      op: "bulk_delete",
      memory_id: MEMORY_ID,
    });
    const message = String(outcome);
    assert.match(message, /caura_manage/);
    // Read from the schema rather than a literal, so this checks the property
    // the message claims — that it names the enum a caller is validated
    // against — instead of pinning that enum a second time.
    const schema = createToolFromSpec("caura_manage").parameters as {
      properties: { op: { enum: string[] } };
    };
    assert.ok(schema.properties.op.enum.length > 0, "no op enum to compare against");
    for (const offered of schema.properties.op.enum) {
      assert.match(message, new RegExp(offered), `message omits offered op ${offered}`);
    }
  });
});

/**
 * Every op both surfaces offer, and the one request each is supposed to make.
 *
 * The guard above only proves an UNRECOGNISED op is refused. It says nothing
 * about a recognised one going to the wrong place, which is the same class of
 * defect one branch further in: `transition` and `update` are both PATCH on
 * neighbouring paths, and `read` and `delete` differ only by method on an
 * identical path. Two ops of caura_doc reach `/documents/query` and
 * `/documents/search` while `read` reaches `/documents/{doc_id}`, so a
 * doc_id of "query" is a legal read that differs from the query op by method
 * alone.
 *
 * Table-driven because the point is COVERAGE of the enum, and the meta-test
 * below fails when an op is added to a surface without a row here — the
 * test-level twin of the `never`-typed fall-through, which fails the build
 * when an op is added without a branch.
 */
const ROUTES: {
  tool: string;
  op: string;
  params: Record<string, unknown>;
  expect: string;
}[] = [
  // caura_manage
  { tool: "caura_manage", op: "read", params: { memory_id: MEMORY_ID },
    expect: `GET /api/v1/memories/${MEMORY_ID}` },
  { tool: "caura_manage", op: "transition", params: { memory_id: MEMORY_ID, status: "archived" },
    expect: `PATCH /api/v1/memories/${MEMORY_ID}/status` },
  { tool: "caura_manage", op: "delete", params: { memory_id: MEMORY_ID },
    expect: `DELETE /api/v1/memories/${MEMORY_ID}` },
  { tool: "caura_manage", op: "update", params: { memory_id: MEMORY_ID, content: "x" },
    expect: `PATCH /api/v1/memories/${MEMORY_ID}` },
  // caura_doc
  { tool: "caura_doc", op: "write", params: { collection: "c-1", doc_id: "doc-1", data: { a: 1 } },
    expect: "POST /api/v1/documents" },
  { tool: "caura_doc", op: "read", params: { collection: "c-1", doc_id: "doc-1" },
    expect: "GET /api/v1/documents/doc-1" },
  { tool: "caura_doc", op: "query", params: { collection: "c-1", where: {} },
    expect: "POST /api/v1/documents/query" },
  { tool: "caura_doc", op: "search", params: { collection: "c-1", query: "q" },
    expect: "POST /api/v1/documents/search" },
  { tool: "caura_doc", op: "list_collections", params: {},
    expect: "GET /api/v1/documents/collections" },
  { tool: "caura_doc", op: "delete", params: { collection: "c-1", doc_id: "doc-1" },
    expect: "DELETE /api/v1/documents/doc-1" },
];

/**
 * Requests against the resource routes, reads included.
 *
 * `mutations()` cannot serve here — half these ops are GETs. Same path filter
 * and the same reason: `apiCall` provisions an agent-scoped credential with
 * its own request when a call carries an `agent_id`, and that route is
 * neither `/memories` nor `/documents`.
 */
function resourceCalls(): string[] {
  return captured
    .filter(
      (c) =>
        c.url.pathname.includes("/memories") || c.url.pathname.includes("/documents"),
    )
    .map((c) => `${c.method} ${c.url.pathname}`);
}

describe("each offered op reaches its own route", () => {
  for (const { tool, op, params, expect } of ROUTES) {
    test(`${tool} op=${op} -> ${expect}`, async () => {
      const outcome = await outcomeOf(tool, { op, ...params });
      assert.ok(
        !(outcome instanceof Error),
        `a legitimate op was refused: ${String(outcome)}`,
      );
      assert.deepEqual(resourceCalls(), [expect]);
    });
  }

  test("the table covers every op each surface offers", () => {
    for (const tool of ["caura_manage", "caura_doc"]) {
      const schema = createToolFromSpec(tool).parameters as {
        properties: { op: { enum: string[] } };
      };
      const covered = ROUTES.filter((r) => r.tool === tool).map((r) => r.op);
      assert.deepEqual(
        [...covered].sort(),
        [...schema.properties.op.enum].sort(),
        `${tool}: the table and the published op enum disagree`,
      );
    }
  });
});
