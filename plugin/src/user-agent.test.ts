/**
 * Tests for the plugin's User-Agent (Caura Heartbeat v1, section 7) and its audit surface.
 *
 * The server counts SDK families from the ``User-Agent`` prefix and matches
 * this plugin on ``openclaw-plugin``. Pin the shape so a refactor cannot
 * silently drop the plugin out of the heartbeat's family breakdown.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  SURFACE,
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
  test("adds User-Agent and the surface to an empty header set", () => {
    assert.deepEqual(withUserAgent(), {
      "User-Agent": USER_AGENT,
      "X-Caura-Surface": "openclaw_plugin",
    });
  });

  test("keeps existing headers intact", () => {
    const headers = withUserAgent({ "Content-Type": "application/json", "X-API-Key": "mc_x" });
    assert.equal(headers["Content-Type"], "application/json");
    assert.equal(headers["X-API-Key"], "mc_x");
    assert.equal(headers["User-Agent"], USER_AGENT);
    assert.equal(headers["X-Caura-Surface"], SURFACE);
  });

  test("returns a fresh object (does not mutate the input)", () => {
    const input = { "X-API-Key": "mc_x" };
    const out = withUserAgent(input);
    assert.notEqual(out, input);
    assert.deepEqual(input, { "X-API-Key": "mc_x" });
  });
});

describe("SURFACE", () => {
  // core-api drops a surface outside its closed set and records null, without
  // an error, so a typo here would go unnoticed. Check against the set itself.
  test("is one of core-api's SURFACES", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../../core-api/src/core_api/audit_actor.py", import.meta.url)),
      "utf-8",
    );
    const match = source.match(/^SURFACES[^=]*=\s*frozenset\(\{([^}]*)\}\)/m);
    assert.ok(match, "SURFACES not found in core_api/audit_actor.py");
    const surfaces = [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    assert.ok(surfaces.includes(SURFACE), `${SURFACE} not in ${surfaces.join(", ")}`);
  });
});
