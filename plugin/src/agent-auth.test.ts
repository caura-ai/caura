import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "mc_test_key_for_agent_auth";
process.env.CAURA_API_URL = "http://localhost:8000";

const originalHome = process.env.HOME;
const tmpHome = mkdtempSync(join(tmpdir(), "agent-auth-test-home-"));
process.env.HOME = tmpHome;
const secretsPath = join(
  tmpHome,
  ".openclaw",
  "plugins",
  FROZEN_PLUGIN_ID,
  ".agent-keys.json",
);
// A directory at the file path makes persistence fail consistently on every
// platform without relying on chmod behavior or elevated-user permissions.
mkdirSync(secretsPath, { recursive: true });

const { resolveAgentKey } = await import("./agent-auth.js");

after(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(tmpHome, { recursive: true, force: true });
});

test("a persistence failure retains the freshly provisioned key in memory", async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const warnings: string[] = [];
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches++;
    return new Response(JSON.stringify({ raw_key: "fresh-agent-key", key_prefix: "fresh" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));

  try {
    assert.equal(await resolveAgentKey("memory-only-agent"), "fresh-agent-key");
    assert.equal(await resolveAgentKey("memory-only-agent"), "fresh-agent-key");
    assert.equal(fetches, 1, "the second lookup must use the in-memory cache");
    assert.ok(warnings.some((line) => line.includes("Could not persist agent key")));
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test("a non-404 provisioning failure is retried on the next cold resolution", async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches++;
    return new Response("unavailable", { status: 503 });
  }) as typeof fetch;
  console.warn = () => {};

  try {
    assert.equal(await resolveAgentKey("transient-agent"), null);
    assert.equal(await resolveAgentKey("transient-agent"), null);
    assert.equal(fetches, 2, "a transient failure must not disable provisioning");
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

// Must stay last: a 404 disables provisioning for the rest of the process.
test("a 404 from the provision route stops further provisioning attempts", async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const originalInfo = console.info;
  const warnings: string[] = [];
  const infos: string[] = [];
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches++;
    return new Response("Not Found", { status: 404 });
  }) as typeof fetch;
  console.warn = (...args: unknown[]) => warnings.push(args.map(String).join(" "));
  console.info = (...args: unknown[]) => infos.push(args.map(String).join(" "));

  try {
    assert.equal(await resolveAgentKey("no-route-agent"), null);
    assert.equal(await resolveAgentKey("no-route-agent"), null);
    assert.equal(await resolveAgentKey("another-agent"), null);
    assert.equal(fetches, 1, "the route is missing server-wide, so it is asked once");
    assert.equal(infos.length, 1, "the fallback is reported once");
    assert.equal(warnings.length, 0, "a missing route is not a warning");
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
    console.info = originalInfo;
  }
});
