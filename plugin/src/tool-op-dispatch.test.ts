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

// `caura_doc op=delete` was the fall-through, and nothing else in the suite
// issues a caura_doc request at all — so a bad conversion of that branch would
// otherwise go unnoticed. `caura_manage op=update` needs no twin here:
// tool-identity.test.ts already drives it and asserts the PATCH.
test("caura_doc op=delete still DELETEs the document", async () => {
  await outcomeOf("caura_doc", { op: "delete", doc_id: "doc-1", collection: "c-1" });
  assert.deepEqual(mutations(), ["DELETE /api/v1/documents/doc-1"]);
});
