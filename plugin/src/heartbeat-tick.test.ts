/**
 * What one heartbeat tick costs the gateway it runs in.
 *
 * - L-56: index.ts drives ``sendHeartbeat`` with ``setInterval``, which does not
 *   wait for the async tick. A tick parked on a slow command (an interview
 *   submit waits up to 300s) let later ticks start beside it, each rerunning the
 *   skill reconciler against the same directories and posting another heartbeat.
 * - L-197: every tick ran ``openclaw --version`` with ``execSync``, blocking the
 *   gateway's event loop for up to 3s a minute to re-read a value that cannot
 *   change while the process runs.
 * - L-198: every tenth tick probed reachability with a real ``POST /search``: a
 *   scored search over the whole tenant, metered on the platform, for one bit.
 * - And the L-56 fix must not trade one problem for another: a tick that waited
 *   for its slow command would hold every later heartbeat back, and the server
 *   marks a node stale after 90s without one.
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

process.env.CAURA_API_URL = "https://heartbeat-tick.test";
process.env.CAURA_API_KEY = "mc_test_key_for_heartbeat_ticks";
process.env.CAURA_TENANT_ID = "t_tick";
process.env.CAURA_NODE_NAME = "node-tick-test";
const originalHome = process.env.HOME;
const originalPath = process.env.PATH;
const tmpHome = mkdtempSync(join(tmpdir(), "heartbeat-tick-home-"));
process.env.HOME = tmpHome;
mkdirSync(join(tmpHome, ".openclaw", "plugins", FROZEN_PLUGIN_ID), { recursive: true });

// A stand-in ``openclaw`` CLI, first on PATH, that records each run.
const bin = join(tmpHome, "bin");
mkdirSync(bin);
const runs = join(tmpHome, "openclaw-runs");
writeFileSync(runs, "");
writeFileSync(
  join(bin, "openclaw"),
  `#!/bin/sh\necho run >> "${runs}"\necho "OpenClaw 2026.5.4"\n`,
  { mode: 0o755 },
);
process.env.PATH = `${bin}:${originalPath ?? ""}`;

const origWarn = console.warn;
const origLog = console.log;
const origError = console.error;
console.warn = () => {};
console.log = () => {};
console.error = () => {};
const { sendHeartbeat } = await import("./heartbeat.js");

interface Call {
  method: string;
  path: string;
  body?: Record<string, unknown>;
}
const calls: Call[] = [];
// Commands the next heartbeat response carries.
let deliver: Array<{ id: string; command: string }> = [];
// While set, a command's result post waits for it; resultPosted fires first.
let resultGate: Promise<void> | undefined;
let resultPosted: (() => void) | undefined;
const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(String(input));
  const method = init?.method ?? "GET";
  calls.push({
    method,
    path: url.pathname,
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
  });
  if (url.pathname.endsWith("/result") && resultGate) {
    resultPosted?.();
    await resultGate;
  }
  let payload: unknown = { ok: true };
  if (url.pathname.endsWith("/fleet/heartbeat")) {
    payload = { commands: deliver };
    deliver = [];
  }
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
  console.warn = origWarn;
  console.log = origLog;
  console.error = origError;
  process.env.PATH = originalPath;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(tmpHome, { recursive: true, force: true });
});

function heartbeatPosts(): Call[] {
  return calls.filter((c) => c.method === "POST" && c.path.endsWith("/fleet/heartbeat"));
}

test("L-56: a tick that starts while another runs does not run a second heartbeat", async () => {
  calls.length = 0;
  // The second call starts while the first is still awaiting its requests,
  // as a setInterval tick does beside one parked on a slow command.
  const first = sendHeartbeat();
  const second = sendHeartbeat();
  await Promise.all([first, second]);

  assert.equal(heartbeatPosts().length, 1, "the overlapping tick posted its own heartbeat");
  assert.equal(
    calls.filter((c) => c.path.endsWith("/skills/installable")).length,
    1,
    "the overlapping tick reran the skill reconciler",
  );
});

/** Whether ``promise`` settles within ``ms``. */
async function settlesWithin(promise: Promise<unknown>, ms: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<boolean>((resolve) => {
    timer = setTimeout(() => resolve(false), ms);
  });
  try {
    return await Promise.race([promise.then(() => true), late]);
  } finally {
    clearTimeout(timer);
  }
}

test("a slow command does not hold back the next heartbeat", async () => {
  calls.length = 0;
  let release!: () => void;
  resultGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const parked = new Promise<void>((resolve) => {
    resultPosted = resolve;
  });
  deliver = [{ id: "88888888-8888-4888-8888-888888888888", command: "ping" }];

  const first = sendHeartbeat();
  await parked; // the command is running, stuck on its result post
  const finished = await settlesWithin(first, 1000);
  const second = sendHeartbeat();
  await settlesWithin(second, 1000);
  release();
  await Promise.all([first, second]);
  resultGate = undefined;
  resultPosted = undefined;

  assert.equal(finished, true, "the tick waited for the command it delivered");
  assert.equal(heartbeatPosts().length, 2, "no heartbeat went out while the command ran");
});

test("L-197: the OpenClaw version is read once per process, not on every tick", async () => {
  calls.length = 0;
  await sendHeartbeat();
  await sendHeartbeat();

  const versions = heartbeatPosts().map((c) => c.body?.openclaw_version);
  assert.deepEqual(versions, ["OpenClaw 2026.5.4", "OpenClaw 2026.5.4"]);
  const ran = readFileSync(runs, "utf-8").split("\n").filter(Boolean).length;
  assert.equal(ran, 1, `openclaw --version ran ${ran} times in this process`);
});

test("L-198: the reachability probe asks /whoami, not a tenant-wide /search", async () => {
  calls.length = 0;
  // The probe runs on every tenth tick; ten ticks reach one wherever the
  // counter stands.
  for (let i = 0; i < 10; i++) await sendHeartbeat();

  const searches = calls.filter((c) => c.path.endsWith("/search"));
  assert.deepEqual(searches, [], "the probe ran a search");
  const probes = calls.filter((c) => c.method === "GET" && c.path.endsWith("/whoami"));
  assert.equal(probes.length, 1, "ten ticks ran exactly one probe");
});
