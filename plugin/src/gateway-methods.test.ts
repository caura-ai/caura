/**
 * Dual-read alias contract for the six gateway RPC commands (rebrand
 * transition, docs/plans/gateway-rpc-dual-read-alias.md).
 *
 * `caura.status`/`.deploy`/`.deploy.status`/`.educate`/`.allowlist.check`/
 * `.allowlist.fix` are canonical; the historical `memclaw.*` spellings  // legacy-name-ok: rule 3 compat-alias test, dual-read alias
 * must keep dispatching to the exact same handler. Unlike the MCP
 * tool-name shim (a prefix-translation layer at dispatch time),
 * `registerGatewayMethod` takes an exact string per call, so this is
 * two separate registrations sharing one function reference — these
 * tests prove they actually share it, not two independently-written
 * copies that could silently drift apart.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import cauraPlugin from "./index.js";

const DUAL_READ_PAIRS = [
  ["caura.status", "memclaw.status"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
  ["caura.deploy", "memclaw.deploy"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
  ["caura.deploy.status", "memclaw.deploy.status"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
  ["caura.educate", "memclaw.educate"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
  ["caura.allowlist.check", "memclaw.allowlist.check"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
  ["caura.allowlist.fix", "memclaw.allowlist.fix"], // legacy-name-ok: rule 3 compat-alias test, dual-read alias
];

function captureGatewayMethods(): Map<string, (...args: any[]) => any> {
  const registered = new Map<string, (...args: any[]) => any>();
  const api = {
    registerTool: () => {},
    registerGatewayMethod: (name: string, handler: (...args: any[]) => any) => {
      registered.set(name, handler);
    },
    registerMemoryPromptSection: () => {},
    registerMemoryFlushPlan: () => {},
    registerMemoryRuntime: () => {},
    registerContextEngine: () => {},
    on: () => {},
  };
  cauraPlugin.register(api);
  return registered;
}

describe("gateway RPC dual-read alias", () => {
  test("all six commands are registered under both the canonical and historical name", () => {
    const registered = captureGatewayMethods();
    for (const [canonical, legacy] of DUAL_READ_PAIRS) {
      assert.ok(registered.has(canonical), `${canonical} was not registered`);
      assert.ok(registered.has(legacy), `${legacy} was not registered`);
    }
  });

  test("each pair shares the exact same handler reference, not two independent copies", () => {
    const registered = captureGatewayMethods();
    for (const [canonical, legacy] of DUAL_READ_PAIRS) {
      assert.equal(
        registered.get(canonical),
        registered.get(legacy),
        `${canonical} and ${legacy} must dispatch to the same function — ` +
          "a copy-pasted second handler could silently drift from the first",
      );
    }
  });

  test("no other gateway method name leaked in besides the six known pairs", () => {
    const registered = captureGatewayMethods();
    const expected = new Set(DUAL_READ_PAIRS.flat());
    for (const name of registered.keys()) {
      assert.ok(expected.has(name), `unexpected gateway method registered: ${name}`);
    }
    assert.equal(registered.size, expected.size);
  });
});
