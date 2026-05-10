/**
 * Tests for the deploy cooldown + post-restart verification (CAURA-444).
 *
 * The cooldown machinery is what stops a broken release from looping
 * forever after the auto-upgrade trigger queues a deploy command.
 * These tests pin the file lifecycle and the isBlocked semantics so
 * future refactors don't accidentally remove the safety net.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, existsSync, rmSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

import { __DEPLOY_INTERNALS__ } from "./heartbeat.js";
import { PLUGIN_VERSION } from "./version.js";

describe("deploy cooldown lifecycle", () => {
  let tmpHome: string;
  let prevHome: string | undefined;

  beforeEach(() => {
    // The cooldown / pending files live under getPluginDir() which
    // resolves from $HOME by default — point it at a clean tmp dir
    // for each test.
    tmpHome = mkdtempSync(join(tmpdir(), "memclaw-deploy-test-"));
    mkdirSync(join(tmpHome, ".openclaw", "plugins", "memclaw"), {
      recursive: true,
    });
    prevHome = process.env.HOME;
    process.env.HOME = tmpHome;
  });

  afterEach(() => {
    process.env.HOME = prevHome;
    try {
      rmSync(tmpHome, { recursive: true, force: true });
    } catch {
      // Best-effort
    }
  });

  test("readCooldown returns empty when no file exists", () => {
    const cd = __DEPLOY_INTERNALS__.readCooldown();
    assert.deepEqual(cd, {});
  });

  test("writeCooldown then readCooldown round-trips fields", () => {
    __DEPLOY_INTERNALS__.writeCooldown("2.4.0", "build-failed");
    const cd = __DEPLOY_INTERNALS__.readCooldown();
    assert.equal(cd.failed_version, "2.4.0");
    assert.ok(typeof cd.blocked_until === "number" && cd.blocked_until > Date.now());
  });

  test("isBlocked returns true for the failed version within cooldown window", () => {
    __DEPLOY_INTERNALS__.writeCooldown("2.4.0", "build-failed");
    const r = __DEPLOY_INTERNALS__.isBlocked("2.4.0");
    assert.equal(r.blocked, true);
    assert.ok(r.until && r.until > Date.now());
  });

  test("isBlocked returns false for a DIFFERENT version (newer hotfix can land)", () => {
    __DEPLOY_INTERNALS__.writeCooldown("2.4.0", "build-failed");
    // A subsequent v2.4.1 must NOT be blocked by the v2.4.0 failure.
    const r = __DEPLOY_INTERNALS__.isBlocked("2.4.1");
    assert.equal(r.blocked, false);
  });

  test("clearCooldown removes the file", () => {
    __DEPLOY_INTERNALS__.writeCooldown("2.4.0", "build-failed");
    __DEPLOY_INTERNALS__.clearCooldown();
    assert.deepEqual(__DEPLOY_INTERNALS__.readCooldown(), {});
  });

  test("failureCooldownHours honours env override", () => {
    const prev = process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS;
    try {
      process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS = "6";
      assert.equal(__DEPLOY_INTERNALS__.failureCooldownHours(), 6);
      process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS = "";
      assert.equal(__DEPLOY_INTERNALS__.failureCooldownHours(), 24);
      process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS = "garbage";
      assert.equal(__DEPLOY_INTERNALS__.failureCooldownHours(), 24);
      process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS = "0";
      assert.equal(__DEPLOY_INTERNALS__.failureCooldownHours(), 24);
    } finally {
      if (prev === undefined) {
        delete process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS;
      } else {
        process.env.MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS = prev;
      }
    }
  });
});

describe("deploy post-restart verification", () => {
  let tmpHome: string;
  let prevHome: string | undefined;

  beforeEach(() => {
    tmpHome = mkdtempSync(join(tmpdir(), "memclaw-deploy-test-"));
    mkdirSync(join(tmpHome, ".openclaw", "plugins", "memclaw"), {
      recursive: true,
    });
    prevHome = process.env.HOME;
    process.env.HOME = tmpHome;
    __DEPLOY_INTERNALS__.resetPostRestartCheck();
  });

  afterEach(() => {
    process.env.HOME = prevHome;
    try { rmSync(tmpHome, { recursive: true, force: true }); } catch { /* noop */ }
  });

  test("no-op when no .deploy-pending.json exists", () => {
    // Fresh boot, no prior deploy attempt — verifier is a no-op and
    // does NOT create a cooldown file.
    __DEPLOY_INTERNALS__.verifyPostRestart();
    assert.deepEqual(__DEPLOY_INTERNALS__.readCooldown(), {});
    assert.deepEqual(__DEPLOY_INTERNALS__.readPending(), {});
  });

  test("clears pending + cooldown on version match (success path)", () => {
    // Stamp pending with the CURRENT version → new boot is on target → success
    __DEPLOY_INTERNALS__.writePending(PLUGIN_VERSION);
    // Pre-existing cooldown from a prior failure should also clear.
    __DEPLOY_INTERNALS__.writeCooldown(PLUGIN_VERSION, "previous-failure");

    __DEPLOY_INTERNALS__.verifyPostRestart();

    assert.deepEqual(__DEPLOY_INTERNALS__.readPending(), {});
    assert.deepEqual(__DEPLOY_INTERNALS__.readCooldown(), {});
  });

  test("engages cooldown on version MISMATCH (drift-2 detection)", () => {
    // Stamp pending with a fictional newer version that the running
    // process did NOT pick up — simulates the drift-2 scenario where
    // version.ts wasn't refreshed on deploy and PLUGIN_VERSION is stale.
    __DEPLOY_INTERNALS__.writePending("99.0.0");

    __DEPLOY_INTERNALS__.verifyPostRestart();

    const cd = __DEPLOY_INTERNALS__.readCooldown();
    assert.equal(cd.failed_version, "99.0.0");
    assert.ok(cd.blocked_until && cd.blocked_until > Date.now());
    // Pending marker is cleared either way — success or failure.
    assert.deepEqual(__DEPLOY_INTERNALS__.readPending(), {});
  });

  test("only runs once per process (postRestartCheckDone flag)", () => {
    __DEPLOY_INTERNALS__.writePending("99.0.0");
    __DEPLOY_INTERNALS__.verifyPostRestart();
    // Cooldown was written. Now write another pending file.
    __DEPLOY_INTERNALS__.clearCooldown();
    __DEPLOY_INTERNALS__.writePending("88.0.0");
    // Calling verifyPostRestart again should be a no-op — the flag
    // prevents re-running. So the second pending stays put.
    __DEPLOY_INTERNALS__.verifyPostRestart();
    assert.equal(__DEPLOY_INTERNALS__.readPending().target_version, "88.0.0");
    assert.deepEqual(__DEPLOY_INTERNALS__.readCooldown(), {});
  });
});
