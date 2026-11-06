/**
 * A tool call acts only as the agent running it.
 *
 * The model fills a tool's arguments, and the ``agent_id`` among them picked
 * the credential the plugin sent the call with: with a tenant key installed,
 * an agent key provisioned for whatever id was named. So the model chose
 * whose trust and private memories the call had. The id now comes from the
 * run OpenClaw built the tool for, and a different one is refused before any
 * request, so no key is provisioned for it.
 *
 * Separate file because CAURA_* settings are read at env.js import time, and
 * HOME points at a throwaway OpenClaw install whose openclaw.json each test
 * writes.
 */
import { test, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_URL = "https://run-identity.test";
process.env.CAURA_API_KEY = "mc_test_tenant_key";
process.env.CAURA_TENANT_ID = "t-run-identity";
delete process.env.CAURA_AGENT_ID;
delete process.env.MEMCLAW_AGENT_ID; // legacy-name-ok: the dual-read alias must be cleared too
const originalHome = process.env.HOME;
const home = mkdtempSync(join(tmpdir(), "caura-run-identity-"));
process.env.HOME = home;
mkdirSync(join(home, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

const origLog = console.log;
const origWarn = console.warn;
console.log = () => {};
console.warn = () => {};
const { createToolFromSpec } = await import("./tool-definitions.js");
const { getDefaultAgentId } = await import("./install-id.js");

interface Sent {
  path: string;
  query: URLSearchParams;
  body: Record<string, unknown> | undefined;
}

let requests: Sent[] = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(String(input));
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  requests.push({ path: url.pathname, query: url.searchParams, body });
  const payload = url.pathname.endsWith("/admin/agent-keys/provision")
    ? { raw_key: `mc_agent_${body?.agent_id}`, key_prefix: "mc_agent" }
    : { id: "m1" };
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
  console.log = origLog;
  console.warn = origWarn;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

function openclawJson(agentsList?: object[]): void {
  const config = agentsList ? { agents: { list: agentsList } } : {};
  writeFileSync(join(home, ".openclaw", "openclaw.json"), JSON.stringify(config));
}

beforeEach(() => {
  openclawJson([{ id: "ops" }, { id: "dev" }]);
  requests = [];
});

const OPS_SESSION = { sessionKey: "agent:ops:slack:C1" };
const MAIN_SESSION = { sessionKey: "agent:main:telegram:direct:42" };

async function call(
  tool: string,
  params: Record<string, unknown>,
  run?: { sessionKey?: string },
): Promise<unknown> {
  return createToolFromSpec(tool, run).execute("call-1", params, undefined as never);
}

/** The operation request: a provisioning POST may come first. */
function operation(): Sent {
  const ops = requests.filter((r) => !r.path.endsWith("/admin/agent-keys/provision"));
  assert.equal(ops.length, 1, `one operation request, got ${ops.map((r) => r.path).join(", ")}`);
  return ops[0];
}

test("a listed agent's session sends its tool calls as that agent", async () => {
  await call("caura_write", { content: "the deploy window moved to thursday" }, OPS_SESSION);
  assert.equal(operation().body?.agent_id, "ops");
});

test("another agent's id is refused before any request or key", async () => {
  const attempts: Array<[string, Record<string, unknown>]> = [
    ["caura_write", { agent_id: "dev", content: "x".repeat(20) }],
    ["caura_recall", { agent_id: "dev", query: "anything" }],
    ["caura_recall", { caller_agent_id: "dev", query: "anything" }],
    ["caura_list", { agent_id: "dev" }],
    ["caura_tune", { agent_id: "dev", top_k: 5 }],
    ["caura_manage", {
      op: "update", memory_id: "11111111-1111-4111-8111-111111111111", agent_id: "dev", content: "y",
    }],
    ["caura_doc", { op: "write", collection: "runbooks", doc_id: "d1", data: {}, agent_id: "dev" }],
  ];
  for (const [tool, params] of attempts) {
    requests = [];
    await assert.rejects(call(tool, params, OPS_SESSION), /not the agent running this call/, tool);
    assert.deepEqual(requests.map((r) => r.path), [], `${tool} reached the network`);
  }
});

test("the agent's own id is kept", async () => {
  await call("caura_write", { agent_id: "ops", content: "x".repeat(20) }, OPS_SESSION);
  assert.equal(operation().body?.agent_id, "ops");
});

test("no other argument names the agent either", async () => {
  for (const steer of [{ agentName: "dev" }, { agent: { id: "dev" } }, { sessionKey: "agent:dev:x" }]) {
    requests = [];
    await call("caura_write", { content: "x".repeat(20), ...steer }, OPS_SESSION);
    assert.equal(operation().body?.agent_id, "ops", JSON.stringify(steer));
  }
});

test("on an install with one agent, 'main' is that agent", async () => {
  openclawJson();
  await call("caura_write", { agent_id: "main", content: "x".repeat(20) }, MAIN_SESSION);
  assert.equal(operation().body?.agent_id, getDefaultAgentId());
  assert.match(getDefaultAgentId(), /^main-[0-9a-f]{12}$/);
});

test("with no session, only the install's own agent, not one it lists", async () => {
  await call("caura_write", { content: "x".repeat(20) });
  assert.equal(operation().body?.agent_id, getDefaultAgentId());
  for (const agent_id of ["dev", "someone-else"]) {
    requests = [];
    await assert.rejects(
      call("caura_write", { agent_id, content: "x".repeat(20) }),
      /not the agent running this call/,
      agent_id,
    );
    assert.deepEqual(requests, [], agent_id);
  }
});
