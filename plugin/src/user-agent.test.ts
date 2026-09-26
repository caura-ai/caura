/**
 * Tests for the plugin's User-Agent (Caura Heartbeat v1, section 7).
 *
 * The server counts SDK families from the ``User-Agent`` prefix and matches
 * this plugin on ``openclaw-plugin``. Pin the shape so a refactor cannot
 * silently drop the plugin out of the heartbeat's family breakdown.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  USER_AGENT,
  USER_AGENT_PREFIX,
  buildUserAgent,
  withUserAgent,
} from "./user-agent.js";
import { PLUGIN_VERSION } from "./version.js";

describe("USER_AGENT", () => {
  test("starts with openclaw-plugin/<PLUGIN_VERSION>", () => {
    assert.equal(USER_AGENT_PREFIX, "openclaw-plugin");
    assert.ok(
      USER_AGENT.startsWith(`openclaw-plugin/${PLUGIN_VERSION}`),
      `expected prefix openclaw-plugin/${PLUGIN_VERSION}, got ${USER_AGENT}`,
    );
  });

  test("carries the running Node major in a (node/<major>) tag", () => {
    const major = process.versions.node.split(".")[0];
    assert.equal(USER_AGENT, `openclaw-plugin/${PLUGIN_VERSION} (node/${major})`);
  });

  test("is a single header-safe line", () => {
    assert.doesNotMatch(USER_AGENT, /[\r\n]/);
    assert.equal(USER_AGENT, USER_AGENT.trim());
  });
});

describe("buildUserAgent", () => {
  test("omits the node tag when no Node version is available", () => {
    assert.equal(buildUserAgent(undefined), `openclaw-plugin/${PLUGIN_VERSION}`);
    assert.equal(buildUserAgent(""), `openclaw-plugin/${PLUGIN_VERSION}`);
  });

  test("keeps only the major component of the Node version", () => {
    assert.equal(buildUserAgent("22.11.0"), `openclaw-plugin/${PLUGIN_VERSION} (node/22)`);
  });
});

describe("withUserAgent", () => {
  test("adds User-Agent to an empty header set", () => {
    assert.deepEqual(withUserAgent(), { "User-Agent": USER_AGENT });
  });

  test("keeps existing headers intact", () => {
    const headers = withUserAgent({ "Content-Type": "application/json", "X-API-Key": "mc_x" });
    assert.equal(headers["Content-Type"], "application/json");
    assert.equal(headers["X-API-Key"], "mc_x");
    assert.equal(headers["User-Agent"], USER_AGENT);
  });

  test("returns a fresh object (does not mutate the input)", () => {
    const input = { "X-API-Key": "mc_x" };
    const out = withUserAgent(input);
    assert.notEqual(out, input);
    assert.deepEqual(input, { "X-API-Key": "mc_x" });
  });
});
