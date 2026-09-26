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
