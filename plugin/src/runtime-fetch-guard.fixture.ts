/** Suite-wide tripwire, including replacement mocks and Node subprocesses. */
import assert from "node:assert/strict";
import {after} from "node:test";
import cp from "node:child_process";
import {syncBuiltinESMExports} from "node:module";
const option = `--import=${import.meta.url}`;
// Existing component fixtures explicitly exercise OSS; integration children supply their own mode.
if (process.argv[1]?.endsWith(".test.js")) process.env.CAURA_AUTH_MODE ??= "standalone";
if (!(process.env.NODE_OPTIONS || "").includes(option))
  process.env.NODE_OPTIONS = `${process.env.NODE_OPTIONS || ""} ${option}`.trim();
let forbidden = 0;
function wrap(implementation: typeof fetch): typeof fetch {
  return async (input, init) => {
    const path = new URL(input instanceof Request ? input.url : String(input)).pathname;
    if (path.endsWith("/admin/agent-keys/provision")) {
      forbidden++; process.exitCode = 1;
      throw new Error("legacy_issuer_request_forbidden");
    }
    return implementation(input, init);
  };
}
let current = wrap(globalThis.fetch);
Object.defineProperty(globalThis, "fetch", {configurable: false,
  get: () => current, set: (value: typeof fetch) => {current = wrap(value);}});
for (const name of ["spawn", "spawnSync", "execFile", "execFileSync", "fork"] as const) {
  const original = cp[name] as (...args: any[]) => any;
  (cp as any)[name] = (...args: any[]) => {
    if (name === "fork" || args[0] === process.execPath || args[0] === "node") {
      const index = Array.isArray(args[1]) ? 2 : 1, options = args[index];
      if (options && typeof options === "object" && options.env) {
        const nodeOptions = String(options.env.NODE_OPTIONS ?? process.env.NODE_OPTIONS ?? "");
        args[index] = {...options, env: {CAURA_AUTH_MODE: "standalone", ...options.env,
          NODE_OPTIONS: nodeOptions.includes(option) ? nodeOptions : `${nodeOptions} ${option}`.trim()}};
      }
    }
    return original(...args);
  };
}
syncBuiltinESMExports();
if (process.argv[1]?.endsWith(".test.js"))
  after(() => assert.equal(forbidden, 0, "no test request may reach the retired issuer, even if caught"));
