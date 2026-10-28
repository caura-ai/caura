/**
 * L-159: CAURA_AGENT_ID set in the plugin's own config takes effect.
 *
 * openclaw.plugin.json declares CAURA_AGENT_ID (and its legacy alias) in
 * ``configSchema``, so OpenClaw offers it as plugin config under
 * ``plugins.entries.<id>.config`` and hands that block to ``register(api)`` as
 * ``api.pluginConfig``. The plugin never read it: the id came only from the
 * environment, so a value set where the manifest says to put it was ignored
 * without a word, and the plugin object's own ``configSchema`` declared nothing.
 *
 * Separate file because CAURA_* settings are captured at env.js import time
 * (node --test runs each file in its own process).
 */
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_URL = "https://plugin-config.test";
process.env.CAURA_API_KEY = "test-key";
process.env.CAURA_TENANT_ID = "t-plugin-config";
// No node name, so register() starts no heartbeat timer in this process.
delete process.env.CAURA_NODE_NAME;
delete process.env.MEMCLAW_NODE_NAME; // legacy-name-ok: the dual-read alias must be cleared too
delete process.env.CAURA_AGENT_ID;
delete process.env.MEMCLAW_AGENT_ID; // legacy-name-ok: the dual-read alias must be cleared too
const originalHome = process.env.HOME;
const home = mkdtempSync(join(tmpdir(), "caura-plugin-config-"));
process.env.HOME = home;
mkdirSync(join(home, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

const origWarn = console.warn;
const origLog = console.log;
const origError = console.error;
console.warn = () => {};
console.log = () => {};
console.error = () => {};
const originalFetch = globalThis.fetch;
globalThis.fetch = (async () =>
  new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })) as typeof fetch;

const { default: cauraPlugin } = await import("./index.js");
const { resolveAgentIdQuiet } = await import("./resolve-agent.js");

after(() => {
  globalThis.fetch = originalFetch;
  console.warn = origWarn;
  console.log = origLog;
  console.error = origError;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

/** register() as OpenClaw calls it, with this plugin's config block. */
function register(pluginConfig?: Record<string, unknown>): void {
  cauraPlugin.register({
    pluginConfig,
    registerTool: () => {},
    registerGatewayMethod: () => {},
    registerMemoryPromptSection: () => {},
    registerMemoryFlushPlan: () => {},
    registerMemoryRuntime: () => {},
    registerContextEngine: () => {},
    on: () => {},
  });
}

test("CAURA_AGENT_ID from the plugin config is the install's agent id", () => {
  register({ CAURA_AGENT_ID: "vm-01" });
  assert.equal(resolveAgentIdQuiet({}), "vm-01");
});

test("the legacy key in the plugin config is read too", () => {
  register({ MEMCLAW_AGENT_ID: "vm-legacy" }); // legacy-name-ok: the documented config alias
  assert.equal(resolveAgentIdQuiet({}), "vm-legacy");
});

test("CAURA_AGENT_ID wins over the legacy key, as it does in .env", () => {
  register({ CAURA_AGENT_ID: "vm-new", MEMCLAW_AGENT_ID: "vm-old" }); // legacy-name-ok: alias precedence
  assert.equal(resolveAgentIdQuiet({}), "vm-new");
});

interface ConfigSchema {
  properties?: Record<string, { type?: string | string[] }>;
}

/** Each declared key with its type; the manifest alone carries descriptions. */
function keyTypes(schema: ConfigSchema): Record<string, string | string[] | undefined> {
  return Object.fromEntries(
    Object.entries(schema.properties ?? {}).map(([key, prop]) => [key, prop.type]),
  );
}

test("the plugin object declares the config keys the manifest does", () => {
  const manifest = JSON.parse(
    readFileSync(join(import.meta.dirname, "..", "openclaw.plugin.json"), "utf-8"),
  );
  assert.deepEqual(
    keyTypes(cauraPlugin.configSchema as ConfigSchema),
    keyTypes(manifest.configSchema),
  );
});

// The automatic-capture switch is read from the same config block, so an
// operator who turns it off where the manifest says is obeyed (the value there
// wins over .env, as CAURA_AGENT_ID's does).
test("CAURA_AUTO_WRITE_TURNS in the plugin config turns automatic capture off", async () => {
  const env = await import("./env.js");
  try {
    for (const off of [false, "false", "0", "no", "off", "OFF"]) {
      register({ CAURA_AUTO_WRITE_TURNS: true });
      assert.equal(env.CAURA_AUTO_WRITE_TURNS, true);
      register({ CAURA_AUTO_WRITE_TURNS: off });
      assert.equal(env.CAURA_AUTO_WRITE_TURNS, false, `config value ${JSON.stringify(off)}`);
    }
  } finally {
    register({ CAURA_AUTO_WRITE_TURNS: true });
  }
});

test("the manifest documents the automatic-capture switch", () => {
  const manifest = JSON.parse(
    readFileSync(join(import.meta.dirname, "..", "openclaw.plugin.json"), "utf-8"),
  );
  const prop = manifest.configSchema.properties.CAURA_AUTO_WRITE_TURNS;
  assert.ok(prop, "configSchema does not declare CAURA_AUTO_WRITE_TURNS");
  for (const spelling of ["false", "0", "no", "off"]) {
    assert.match(prop.description, new RegExp(`\\b${spelling}\\b`));
  }
});
