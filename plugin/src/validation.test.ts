/**
 * Tests for `verifyCommandSignature` — the HMAC fleet-command verifier.
 *
 * Three modes that need to stay correct:
 *
 *   1. **Tampered**: signature present but doesn't verify → reject
 *      (always; this is the actual security invariant).
 *   2. **Strict** (`requireSigned=true`): missing signature → reject.
 *      Used when the operator has wired the gateway to sign commands.
 *   3. **Permissive** (`requireSigned=false`, default): missing
 *      signature → accept with a one-time warning. Required because the
 *      OSS server doesn't sign commands; the prior strict-by-default
 *      behavior silently broke every educate / install_skill /
 *      uninstall_skill / deploy command on every install with auth on.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { win32 } from "node:path";

import {
  insecureKeyTransportMessage,
  isContainedResolvedPath,
  isLoopbackHost,
  keyTransportPolicy,
  reportKeyTransportPolicy,
  verifyCommandSignature,
} from "./validation.js";

const KEY = "test-hmac-secret";

describe("isContainedPath", () => {
  test("recognizes Windows descendants without accepting siblings or other drives", () => {
    const parent = "C:\\Users\\agent\\workspace";

    assert.equal(
      isContainedResolvedPath(
        "C:\\Users\\agent\\workspace\\project",
        parent,
        win32,
      ),
      true,
    );
    assert.equal(
      isContainedResolvedPath(
        "C:\\Users\\agent\\workspace-copy",
        parent,
        win32,
      ),
      false,
    );
    assert.equal(
      isContainedResolvedPath("D:\\project", parent, win32),
      false,
    );
  });
});

function signedCommand(overrides: Partial<{ id: string; command: string; payload: Record<string, unknown>; timestamp: string }> = {}) {
  const cmd = {
    id: overrides.id ?? "cmd-1",
    command: overrides.command ?? "ping",
    payload: overrides.payload ?? { msg: "hello" },
    timestamp: overrides.timestamp ?? new Date().toISOString(),
  };
  const payloadStr = JSON.stringify(cmd.payload);
  const hmacInput = `${cmd.id}:${cmd.command}:${cmd.timestamp}:${payloadStr}`;
  const signature = createHmac("sha256", KEY).update(hmacInput).digest("hex");
  return { ...cmd, signature };
}

describe("verifyCommandSignature", () => {
  test("keyless mode accepts unsigned commands", () => {
    const cmd = { id: "1", command: "ping" };
    const result = verifyCommandSignature(cmd, "");
    assert.equal(result.valid, true);
    assert.equal(result.reason, "no_secret_configured");
  });

  test("keyless mode rejects a signature it cannot verify", () => {
    const cmd = { id: "1", command: "ping", signature: "deadbeef" };
    const result = verifyCommandSignature(cmd, "");
    assert.equal(result.valid, false);
    assert.equal(result.reason, "no_secret_configured_but_signature_present");
  });

  test("permissive (default) accepts unsigned commands when key is set", () => {
    // This is the fix: prior behavior was missing_signature → reject,
    // which silently broke every fleet command on OSS installs (server
    // doesn't sign).
    const cmd = { id: "1", command: "install_skill" };
    const result = verifyCommandSignature(cmd, KEY);
    assert.equal(result.valid, true);
    assert.equal(result.reason, "unsigned_accepted_permissive");
  });

  test("strict mode rejects unsigned commands when key is set", () => {
    const cmd = { id: "1", command: "install_skill" };
    const result = verifyCommandSignature(cmd, KEY, /* requireSigned */ true);
    assert.equal(result.valid, false);
    assert.equal(result.reason, "missing_signature");
  });

  test("valid signature passes in both modes", () => {
    const cmd = signedCommand();
    assert.equal(verifyCommandSignature(cmd, KEY).valid, true);
    assert.equal(verifyCommandSignature(cmd, KEY, true).valid, true);
  });

  test("tampered signature fails closed regardless of mode", () => {
    const cmd = signedCommand();
    // Flip the last hex char to a DIFFERENT value. The old `.replace(/.$/, "0")`
    // was a 1-in-16 flake: whenever the genuine signature already ended in "0"
    // (timestamp-dependent), the "tampered" copy was byte-identical to the
    // original and verification correctly returned true.
    const tampered = {
      ...cmd,
      signature: cmd.signature.replace(/.$/, (c) => (c === "0" ? "1" : "0")),
    };
    assert.notEqual(tampered.signature, cmd.signature);
    assert.equal(verifyCommandSignature(tampered, KEY).valid, false);
    assert.equal(verifyCommandSignature(tampered, KEY).reason, "invalid_signature");
    assert.equal(verifyCommandSignature(tampered, KEY, true).valid, false);
  });

  test("expired timestamp fails (signed)", () => {
    const ancient = signedCommand({
      timestamp: new Date(Date.now() - 10 * 60_000).toISOString(),
    });
    const result = verifyCommandSignature(ancient, KEY);
    assert.equal(result.valid, false);
    assert.equal(result.reason, "expired_timestamp");
  });

  test("payload mutation invalidates signature", () => {
    const cmd = signedCommand({ payload: { name: "skill-a" } });
    const tampered = { ...cmd, payload: { name: "skill-b" } };
    assert.equal(verifyCommandSignature(tampered, KEY).valid, false);
    assert.equal(verifyCommandSignature(tampered, KEY).reason, "invalid_signature");
  });
});

describe("keyTransportPolicy — API key never crosses the network in cleartext", () => {
  test("loopback plain HTTP is allowed without opt-in", () => {
    for (const url of [
      "http://localhost:8000",
      "http://127.0.0.1:8000",
      "http://127.5.6.7",
      "http://[::1]:8000",
      "http://api.localhost",
    ]) {
      assert.equal(keyTransportPolicy(url, false), "send", url);
    }
  });

  test("https is always allowed", () => {
    assert.equal(keyTransportPolicy("https://10.0.0.5:8000", false), "send");
    assert.equal(keyTransportPolicy("https://caura.example.com", false), "send");
  });

  test("remote plain HTTP is refused without opt-in", () => {
    for (const url of [
      "http://10.0.0.5:8000",
      "http://caura.internal",
      "http://localhost.evil.com",
      "http://128.0.0.1",
      "not a url",
    ]) {
      assert.equal(keyTransportPolicy(url, false), "refuse", url);
    }
  });

  test("remote plain HTTP is sent-insecure with opt-in", () => {
    assert.equal(keyTransportPolicy("http://10.0.0.5:8000", true), "send-insecure");
  });

  test("isLoopbackHost rejects look-alikes", () => {
    assert.equal(isLoopbackHost("localhost"), true);
    assert.equal(isLoopbackHost("[::1]"), true);
    assert.equal(isLoopbackHost("localhost.evil.com"), false);
    assert.equal(isLoopbackHost("127.0.0.1.nip.io"), false);
  });

  test("refusal message names the host and the exact fix", () => {
    const msg = insecureKeyTransportMessage("http://10.0.0.5:8000");
    assert.match(msg, /10\.0\.0\.5:8000/);
    assert.match(msg, /CAURA_ALLOW_INSECURE_HTTP=true/);
    assert.match(msg, /https:\/\//);
  });

  test("import-time report: silent for loopback, error on refuse, warn on opt-in", (t) => {
    const errors: string[] = [];
    const warns: string[] = [];
    t.mock.method(console, "error", (m: string) => errors.push(m));
    t.mock.method(console, "warn", (m: string) => warns.push(m));

    reportKeyTransportPolicy("http://localhost:8000", "k", "send");
    assert.deepEqual([errors.length, warns.length], [0, 0]);

    reportKeyTransportPolicy("http://10.0.0.5:8000", "", "refuse");
    assert.deepEqual([errors.length, warns.length], [0, 0], "no key, nothing to protect");

    reportKeyTransportPolicy("http://10.0.0.5:8000", "k", "refuse");
    assert.equal(errors.length, 1);

    reportKeyTransportPolicy("http://10.0.0.5:8000", "k", "send-insecure");
    assert.equal(warns.length, 1);
    assert.match(warns[0], /CAURA_ALLOW_INSECURE_HTTP/);
    // L-80: the channel carries commands, plugin code and skills, not only the key.
    assert.match(warns[0], /deploy/);
    assert.match(warns[0], /educate/);
    assert.match(warns[0], /skills/);
  });
});
