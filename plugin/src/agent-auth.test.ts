import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
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

test("failed provisioning cools down per agent, then retries and caches success", async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const originalNow = Date.now;
  let now = 1_000;
  let fetches = 0;
  let status = 503;
  globalThis.fetch = (async (_input, init) => {
    assert.equal(init?.redirect, "error", "provisioning must not forward the tenant key on redirects");
    fetches++;
    if (status === 0) throw new TypeError("fetch failed");
    return new Response(status === 200 ? JSON.stringify({ raw_key: "recovered-key", key_prefix: "test" }) : "unavailable", { status });
  }) as typeof fetch;
  console.warn = () => {};
  Date.now = () => now;

  try {
    for (const failure of [401, 403, 503, 0]) {
      status = failure;
      const agent = `retry-agent-${failure}`;
      const before = fetches;
      assert.equal(await resolveAgentKey(agent), null);
      assert.equal(await resolveAgentKey(agent), null);
      now += 59_999;
      assert.equal(await resolveAgentKey(agent), null);
      assert.equal(fetches, before + 1, "calls in the cooldown must not provision again");
      now++;
      status = 200;
      assert.equal(await resolveAgentKey(agent), "recovered-key");
      assert.equal(await resolveAgentKey(agent), "recovered-key");
      assert.equal(fetches, before + 2, "retry succeeds and subsequent calls use the cache");
    }
  } finally {
    Date.now = originalNow;
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test("concurrent cold lookups share one provision per agent", async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  const requested: string[] = [];
  globalThis.fetch = (async (_input, init) => {
    const agent = JSON.parse(String(init?.body)).agent_id as string;
    requested.push(agent);
    await barrier;
    return new Response(JSON.stringify({ raw_key: `key-for-${agent}`, key_prefix: "test" }), { status: 200 });
  }) as typeof fetch;
  console.warn = () => {};
  try {
    const lookups = Array.from({ length: 10 }, () => resolveAgentKey("parallel-a"));
    const other = resolveAgentKey("parallel-b");
    assert.deepEqual(requested, ["parallel-a", "parallel-b"]);
    release();
    assert.deepEqual(await Promise.all(lookups), Array(10).fill("key-for-parallel-a"));
    assert.equal(await other, "key-for-parallel-b");
    assert.equal(await resolveAgentKey("parallel-a"), "key-for-parallel-a");
    assert.equal(requested.length, 2);
  } finally {
    release();
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test("different agents provisioning concurrently keep both persisted keys", async () => {
  rmSync(secretsPath, { recursive: true });
  const originalFetch = globalThis.fetch;
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  globalThis.fetch = (async (_input, init) => {
    const agent = JSON.parse(String(init?.body)).agent_id as string;
    await barrier;
    return new Response(JSON.stringify({ raw_key: `key-for-${agent}`, key_prefix: "test" }), { status: 200 });
  }) as typeof fetch;
  try {
    const pending = [resolveAgentKey("persist-a"), resolveAgentKey("persist-b")];
    release();
    await Promise.all(pending);
    const saved = JSON.parse(readFileSync(secretsPath, "utf8"));
    assert.equal(saved.keys["persist-a"].key, "key-for-persist-a");
    assert.equal(saved.keys["persist-b"].key, "key-for-persist-b");
  } finally {
    release();
    globalThis.fetch = originalFetch;
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
