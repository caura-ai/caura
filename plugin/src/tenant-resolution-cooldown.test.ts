/**
 * M-69: a failed tenant resolution is remembered for a short while.
 *
 * With CAURA_TENANT_ID unset, every lifecycle hook (ingest / assemble /
 * afterTurn) awaits `ensureTenantId`. When `/whoami` fails, one resolution
 * makes four attempts with 2s/4s/8s backoff, and against a half-open backend
 * each attempt waits out its 10s timeout: about 54s. A failure cleared the
 * memoized promise, so the next turn ran the whole loop again, and every turn
 * blocked that long. A turn inside the cooldown after a failure must now fail
 * at once, and the first turn after it must resolve afresh.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

// Before importing env.ts, as env.test.ts does: a key, and no tenant id.
process.env.CAURA_API_KEY = "mc_test_key_for_tenant_cooldown";
delete process.env.CAURA_TENANT_ID;

const { ensureTenantId } = await import("./env.js");

test("a failed resolution fails the next turn at once and is retried after the cooldown", async () => {
  const realFetch = globalThis.fetch;
  const realSetTimeout = globalThis.setTimeout;
  const realNow = Date.now;
  const realWarn = console.warn;
  const realError = console.error;
  let now = realNow();
  let calls = 0;
  Date.now = () => now;
  console.warn = () => {};
  console.error = () => {};
  // Collapse the retry backoff. A plain Error takes the retry path, as a
  // half-open backend's per-attempt timeout does.
  globalThis.setTimeout = ((cb: () => void) => {
    void Promise.resolve().then(cb);
    return 0 as unknown as ReturnType<typeof realSetTimeout>;
  }) as unknown as typeof setTimeout;
  globalThis.fetch = (async () => {
    calls++;
    throw new Error("socket timeout");
  }) as typeof fetch;
  try {
    await assert.rejects(ensureTenantId(), /Failed to resolve tenant_id/);
    assert.equal(calls, 4, "the first turn runs the whole retry loop");

    await assert.rejects(ensureTenantId(), /Failed to resolve tenant_id/);
    assert.equal(calls, 4, "the next turn must fail at once, not run the loop again");

    now += 60_000;
    await assert.rejects(ensureTenantId(), /Failed to resolve tenant_id/);
    assert.equal(calls, 8, "after the cooldown a turn resolves afresh");
  } finally {
    globalThis.fetch = realFetch;
    globalThis.setTimeout = realSetTimeout;
    Date.now = realNow;
    console.warn = realWarn;
    console.error = realError;
  }
});
