import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  getReachability,
  markReachable,
  markUnreachable,
  trackReachability,
  isNetworkClassError,
  _resetReachabilityForTests,
} from "./health.js";

describe("reachability tracker", () => {
  beforeEach(() => _resetReachabilityForTests());

  test("starts unknown", () => {
    assert.equal(getReachability().state, "unknown");
  });

  test("markReachable flips state and stamps lastCheckMs", () => {
    const before = Date.now();
    markReachable();
    const r = getReachability();
    assert.equal(r.state, "reachable");
    assert.ok(r.lastCheckMs >= before);
    assert.equal(r.reason, undefined);
  });

  test("markUnreachable carries the reason forward", () => {
    markUnreachable("pairing required");
    const r = getReachability();
    assert.equal(r.state, "unreachable");
    assert.equal(r.reason, "pairing required");
  });

  test("getReachability returns a copy, not a mutable reference", () => {
    markReachable();
    const snapshot = getReachability() as unknown as Record<string, unknown>;
    snapshot.state = "tampered";
    assert.equal(getReachability().state, "reachable");
  });
});

describe("isNetworkClassError", () => {
  // The tracker must not mark the backend unreachable on server-logic errors
  // (4xx). A 404 on a memory id means the server said "no" — not that we
  // couldn't reach it. Getting this classification wrong would cause the
  // runtime to silently absorb application-level errors into "unreachable".
  test("HTTP 5xx is network-class", () => {
    for (const code of [500, 502, 503, 504]) {
      assert.ok(
        isNetworkClassError(new Error(`http ${code}: something`)),
        `${code} should be network-class`,
      );
    }
  });

  test("HTTP 4xx is NOT network-class", () => {
    for (const code of [400, 401, 403, 404, 409, 422, 429]) {
      assert.ok(
        !isNetworkClassError(new Error(`http ${code}: nope`)),
        `${code} must not be network-class`,
      );
    }
  });

  test("fetch / DNS / TCP / timeout failures are network-class", () => {
    for (const msg of [
      "fetch failed",
      "ECONNREFUSED 127.0.0.1:8080",
      "getaddrinfo ENOTFOUND host",
      "ECONNRESET",
      "ETIMEDOUT",
      "socket hang up",
      "network request failed",
    ]) {
      assert.ok(isNetworkClassError(new Error(msg)), `"${msg}" should be network-class`);
    }
  });

  test("AbortError is NOT network-class (AbortController / lifecycle cancellation)", () => {
    // AbortController.abort() throws DOMException with name === "AbortError".
    // These are triggered by timeouts and OpenClaw lifecycle management, not
    // by a backend outage, and must not flip the reachability tracker.
    const abortErr = new Error("This operation was aborted");
    (abortErr as unknown as { name: string }).name = "AbortError";
    assert.ok(!isNetworkClassError(abortErr), "AbortError by name must not be network-class");

    // Sanity: without the name field, the same message text doesn't spuriously
    // match either (the old `msg.includes("abort")` was removed).
    assert.ok(
      !isNetworkClassError(new Error("operation aborted")),
      "generic 'aborted' messages must not be network-class",
    );
  });

  test("generic/unknown errors are NOT network-class", () => {
    assert.ok(!isNetworkClassError(new Error("Invalid argument")));
    assert.ok(!isNetworkClassError(new Error("permission denied")));
    assert.ok(!isNetworkClassError(null));
    assert.ok(!isNetworkClassError(undefined));
  });
});

describe("trackReachability", () => {
  beforeEach(() => _resetReachabilityForTests());

  test("success marks reachable and returns the value", async () => {
    const r = await trackReachability(async () => "payload");
    assert.equal(r, "payload");
    assert.equal(getReachability().state, "reachable");
  });

  test("network-class failure marks unreachable and re-throws", async () => {
    await assert.rejects(
      trackReachability(async () => {
        throw new Error("fetch failed");
      }),
      /fetch failed/,
    );
    const s = getReachability();
    assert.equal(s.state, "unreachable");
    assert.match(s.reason ?? "", /fetch failed/);
  });

  test("4xx failure re-throws but leaves tracker untouched", async () => {
    markReachable();
    await assert.rejects(
      trackReachability(async () => {
        throw new Error("http 404: not found");
      }),
      /404/,
    );
    // Still reachable — a 404 is a server-logic answer, not a reachability
    // signal.
    assert.equal(getReachability().state, "reachable");
  });

  test("AbortError re-throws but leaves tracker untouched", async () => {
    // Timeouts and lifecycle-cancelled requests surface as AbortError; they
    // are not evidence that the backend is unreachable. Regression against
    // an earlier version that matched the word "abort" in the message.
    markReachable();
    await assert.rejects(
      trackReachability(async () => {
        const e = new Error("This operation was aborted");
        (e as unknown as { name: string }).name = "AbortError";
        throw e;
      }),
    );
    assert.equal(getReachability().state, "reachable");
  });
});
