/**
 * M-107: the default agent writes under the id its heartbeat registers.
 *
 * With no ``agents.list`` in openclaw.json, OpenClaw runs one agent, ``main``,
 * and every session key it passes reads ``agent:main:...``. The heartbeat
 * registered that agent as ``main-<installId>``, while a turn took its identity
 * from the session key and wrote as ``main``: one registered agent that never
 * wrote, and one writing agent that no heartbeat reported.
 *
 * install.json now records the default agent's id. An install whose
 * install.json predates that record has been writing as ``main``, and the
 * plugin's own recall filters on the agent's id, so it keeps ``main``. A new
 * install gets ``main-<installId>``, so installs sharing a tenant stay apart.
 * Either way the heartbeat registers the id the turns write under.
 *
 * Separate file because CAURA_* settings are captured at env.js import time
 * (node --test runs each file in its own process).
 */
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "mc_test_key_for_default_agent_identity";
process.env.CAURA_API_URL = "http://localhost:8000";
process.env.CAURA_TENANT_ID = "t_test";
process.env.CAURA_NODE_NAME = "node-default-agent-test";
delete process.env.CAURA_AGENT_ID;
const originalHome = process.env.HOME;
const home = mkdtempSync(join(tmpdir(), "caura-default-agent-"));
process.env.HOME = home;
const pluginDir = join(home, ".openclaw", "plugins", FROZEN_PLUGIN_ID);
mkdirSync(pluginDir, { recursive: true });

const origWarn = console.warn;
const origLog = console.log;
const origError = console.error;
console.warn = () => {};
console.log = () => {};
console.error = () => {};
const { sendHeartbeat } = await import("./heartbeat.js");
const { resolveAgentId } = await import("./resolve-agent.js");
const { _resetInstallIdCacheForTesting } = await import("./install-id.js");

const heartbeats: Array<{ agents?: Array<{ agentId: string }> }> = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  if (String(input).includes("/fleet/heartbeat")) heartbeats.push(JSON.parse(String(init?.body)));
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
  console.warn = origWarn;
  console.log = origLog;
  console.error = origError;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

function install(state: { installJson?: object; agentsList?: object[] }): void {
  rmSync(join(pluginDir, "install.json"), { force: true });
  if (state.installJson) {
    writeFileSync(join(pluginDir, "install.json"), JSON.stringify(state.installJson));
  }
  const config = state.agentsList ? { agents: { list: state.agentsList } } : {};
  writeFileSync(join(home, ".openclaw", "openclaw.json"), JSON.stringify(config));
  _resetInstallIdCacheForTesting();
  heartbeats.length = 0;
}

async function registered(): Promise<string[]> {
  await sendHeartbeat();
  assert.equal(heartbeats.length, 1, "one heartbeat was sent");
  return (heartbeats[0].agents ?? []).map((a) => a.agentId);
}

const MAIN_SESSION = { sessionKey: "agent:main:telegram:direct:42" };

test("a new install's default agent writes as main-<installId>, the id it registers", async () => {
  install({});
  const writes = resolveAgentId(MAIN_SESSION);
  assert.match(writes, /^main-[0-9a-f]{12}$/, "a new install must not write as the shared main");
  assert.deepEqual(await registered(), [writes]);
});

test("an install from before the record keeps main, and its heartbeat registers main", async () => {
  install({
    installJson: { schema_version: 1, install_id: "0123456789ab", created_at: "2026-06-01T00:00:00.000Z" },
  });
  assert.equal(resolveAgentId(MAIN_SESSION), "main", "its memories and its recall carry main");
  assert.deepEqual(await registered(), ["main"]);
  const state = JSON.parse(readFileSync(join(pluginDir, "install.json"), "utf-8"));
  assert.equal(state.install_id, "0123456789ab", "the install keeps its id");
  assert.equal(state.default_agent_id, "main", "the choice is recorded, so it never changes");
});

test("agents an operator lists keep their ids on both paths", async () => {
  install({ agentsList: [{ id: "main" }, { id: "work" }] });
  assert.equal(resolveAgentId(MAIN_SESSION), "main");
  assert.equal(resolveAgentId({ sessionKey: "agent:work:slack:C1" }), "work");
  assert.deepEqual(await registered(), ["main", "work"]);
});
