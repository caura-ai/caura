/**
 * apiCall against a NON-loopback plain-HTTP backend without the opt-in: the key must never be sent.
 *
 * Separate file because CAURA_API_URL / CAURA_ALLOW_INSECURE_HTTP are
 * captured at env.js import time (node --test runs each file in its own
 * process).
 */
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "mc_secret_must_not_leak";
process.env.CAURA_API_URL = "http://10.0.0.5:8000";
process.env.CAURA_TENANT_ID = "t_test";
delete process.env.CAURA_ALLOW_INSECURE_HTTP;
const originalHome = process.env.HOME;
const tmpHome = mkdtempSync(join(tmpdir(), "transport-insecure-test-home-"));
process.env.HOME = tmpHome;
mkdirSync(join(tmpHome, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

// Capture the import-time report.
const errors: string[] = [];
const warns: string[] = [];
const origError = console.error;
const origWarn = console.warn;
console.error = (...a: unknown[]) => { errors.push(a.map(String).join(" ")); };
console.warn = (...a: unknown[]) => { warns.push(a.map(String).join(" ")); };
const { apiCall } = await import("./transport.js");
console.error = origError;
console.warn = origWarn;

const calls: { url: string; init?: RequestInit }[] = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  calls.push({ url: String(input), init });
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(tmpHome, { recursive: true, force: true });
});

test("refuses the call with an actionable error and never reaches fetch", async () => {
  await assert.rejects(
    () => apiCall("POST", "/search", { q: "x" }),
    (err: Error) => {
      assert.match(err.message, /Refusing to send CAURA_API_KEY to 10\.0\.0\.5:8000/);
      assert.match(err.message, /CAURA_ALLOW_INSECURE_HTTP=true/);
      return true;
    },
  );
  assert.equal(calls.length, 0, "key must not be put on the wire");
});

test("refuses agent-scoped calls before provisioning an agent key", async () => {
  await assert.rejects(() => apiCall("GET", "/memories", undefined, undefined, undefined, "agent-1"));
  assert.equal(calls.length, 0);
});

test("logs one error at import, no warning", () => {
  assert.equal(errors.filter((e) => e.includes("Refusing to send CAURA_API_KEY")).length, 1);
  assert.equal(warns.filter((w) => w.includes("CAURA_ALLOW_INSECURE_HTTP")).length, 0);
});

