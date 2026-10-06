/**
 * apiCall against a NON-loopback plain-HTTP backend WITH CAURA_ALLOW_INSECURE_HTTP: sent, with one warning.
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
process.env.CAURA_ALLOW_INSECURE_HTTP = "true";
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

test("sends the key when explicitly opted in", async () => {
  await apiCall("POST", "/search", { q: "x" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://10.0.0.5:8000/api/v1/search");
  const headers = calls[0].init?.headers as Record<string, string>;
  assert.equal(headers["X-API-Key"], "mc_secret_must_not_leak");
});

test("logs exactly one warning at import, no refusal error", () => {
  assert.equal(warns.filter((w) => w.includes("CAURA_ALLOW_INSECURE_HTTP is set")).length, 1);
  assert.equal(errors.filter((e) => e.includes("Refusing")).length, 0);
});

