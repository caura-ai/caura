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

import { verifyCommandSignature } from "./validation.js";

const KEY = "test-hmac-secret";

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
    const tampered = { ...cmd, signature: cmd.signature.replace(/.$/, "0") };
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
