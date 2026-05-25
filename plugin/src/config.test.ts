import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  isMemorySlotClaimed,
  isContextEngineSlotClaimed,
  isMemclawFullyConfigured,
} from "./config.js";
import { getPluginDir } from "./paths.js";

// Minimal "happy-path" config scaffold — every predicate true. Individual
// tests selectively break one field at a time.
function happyConfig(): Record<string, unknown> {
  return {
    plugins: {
      allow: ["memclaw"],
      entries: { memclaw: { enabled: true } },
      load: { paths: [getPluginDir()] },
      slots: { memory: "memclaw", contextEngine: "memclaw" },
    },
    tools: { alsoAllow: [] },
  };
}

describe("isMemorySlotClaimed", () => {
  test("false when plugins.slots is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots;
    assert.equal(isMemorySlotClaimed(c), false);
  });

  test("false when memory slot is held by a different plugin", () => {
    const c = happyConfig();
    (c as any).plugins.slots.memory = "memory-core";
    assert.equal(isMemorySlotClaimed(c), false);
  });

  test("true when memory slot is memclaw", () => {
    assert.equal(isMemorySlotClaimed(happyConfig()), true);
  });
});

describe("isMemclawFullyConfigured", () => {
  // Paints the Fleet UI dashboard via heartbeat.setup_status.fully_configured.
  // Each test below corresponds to one of the four conditions that must hold.

  test("true on happy-path config", () => {
    assert.equal(isMemclawFullyConfigured(happyConfig()), true);
  });

  test("false when memclaw is not allowlisted", () => {
    const c = happyConfig();
    (c as any).plugins.allow = [];
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when memclaw is disabled", () => {
    const c = happyConfig();
    (c as any).plugins.entries.memclaw.enabled = false;
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when plugin path is not loaded", () => {
    const c = happyConfig();
    (c as any).plugins.load.paths = [];
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when memory slot is not claimed", () => {
    const c = happyConfig();
    (c as any).plugins.slots.memory = "memory-core";
    assert.equal(isMemclawFullyConfigured(c), false);
  });
});


describe("isContextEngineSlotClaimed (CAURA-000 — keystone-injection gate)", () => {
  // OpenClaw 2026.5.4 dist/registry-DFFgCbcm.js:241 resolveContextEngine
  // reads config.plugins.slots.contextEngine. Without it set to "memclaw",
  // OpenClaw uses its default "legacy" engine and our assemble() is never
  // called — so the <keystone_rules> block never reaches the prompt.

  test("false when plugins.slots is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots;
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("false when contextEngine slot held by another plugin (e.g. legacy)", () => {
    const c = happyConfig();
    (c as any).plugins.slots.contextEngine = "legacy";
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("false when contextEngine slot is undefined (the WhatsApp-regression case)", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots.contextEngine;
    assert.equal(isContextEngineSlotClaimed(c), false);
  });

  test("true when contextEngine slot is memclaw", () => {
    assert.equal(isContextEngineSlotClaimed(happyConfig()), true);
  });
});

describe("isMemclawFullyConfigured — contextEngine slot is now required", () => {
  // Pre-fix happyConfig() didn't include contextEngine and isMemclawFullyConfigured
  // returned true anyway. That hid the WhatsApp keystone-injection regression
  // because Fleet UI's "fully configured" badge was green while assemble()
  // silently never ran. Adding the slot to the predicate surfaces the gap.

  test("false when contextEngine slot is missing", () => {
    const c = happyConfig();
    delete (c as any).plugins.slots.contextEngine;
    assert.equal(isMemclawFullyConfigured(c), false);
  });

  test("false when contextEngine slot is held by another plugin", () => {
    const c = happyConfig();
    (c as any).plugins.slots.contextEngine = "legacy";
    assert.equal(isMemclawFullyConfigured(c), false);
  });
});
