/** Integration boundary for the complete runtime switch, never a partial downgrade. */
import {afterEach, test} from "node:test";
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {hostname, tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {FROZEN_PLUGIN_ID} from "./legacy-contracts.fixture.js";
import {WIRING_PRELOAD, WIRING_RUNNER} from "./runtime-wiring.fixture.js";
const DIST = dirname(fileURLToPath(import.meta.url)), SRC = join(DIST, "../src"), ORIGIN = "https://broker.example.test";
const dirs: string[] = [];
afterEach(() => {for (const p of dirs.splice(0)) rmSync(p, {recursive: true, force: true});});
function fixture() {
  const home = mkdtempSync(join(tmpdir(), "runtime-wiring-")); dirs.push(home);
  const dir = join(home, ".openclaw/plugins", FROZEN_PLUGIN_ID); mkdirSync(dir, {recursive: true, mode: 0o700});
  const authority = join(dir, ".installation-authority.json"), cache = join(dir, ".installation-agent-keys.json"), trace = join(home, "trace.jsonl");
  const expiry = new Date(Date.now() + 30 * 86400_000).toISOString(), preload = join(home, "preload.mjs");
  writeFileSync(preload, WIRING_PRELOAD);
  const journal = {schema_version: 1, status: "verified", mode: "register", gateway: ORIGIN, api_prefix: "/api/v1",
    hostname: hostname(), platform: process.platform, arch: process.arch, created_at: new Date().toISOString(),
    expected_tenant: "tenant-a", expected_fleet: "fleet-a", install_uuid: "11111111-1111-4111-8111-111111111111",
    machine_fingerprint: "a".repeat(64), install_credential: "fixture_install_key", tenant_id: "tenant-a", org_id: "org-a", fleet_id: "fleet-a", expires_at: expiry};
  writeFileSync(authority, JSON.stringify(journal), {mode: 0o600});
  const preserved = new Map([[".agent-keys.json", '{"version":1,"keys":{"agent-a":{"key":"fixture_legacy_key"}}}'],
    ["install.json", '{"schema_version":1,"install_id":"abcdef012345","default_agent_id":"agent-a"}']]);
  for (const [name, value] of preserved) writeFileSync(join(dir, name), value, {mode: 0o600});
  writeFileSync(join(home, ".openclaw/openclaw.json"), JSON.stringify({agents: {list: [{id: "agent-a"}, {id: "agent-b"}]}}));
  const calls = () => existsSync(trace) ? readFileSync(trace, "utf8").trim().split("\n").filter(Boolean).map(s => JSON.parse(s)) : [];
  function run({action = "api", mode = "managed", request = {agentId: "agent-a"} as Record<string, unknown>, plan = {}, config = {}, env = {}} = {}) {
    const r = spawnSync(process.execPath, ["--import", pathToFileURL(preload).href, "--input-type=module", "--eval", WIRING_RUNNER], {
      env: {PATH: process.env.PATH, HOME: home, NODE_ENV: "test", CAURA_API_URL: ORIGIN, CAURA_API_KEY: "fixture_tenant_key",
        CAURA_AUTH_MODE: mode, CAURA_TENANT_ID: "tenant-a", CAURA_FLEET_ID: "fleet-a", CAURA_NODE_NAME: action === "heartbeat" || action === "update" ? "fixture-node" : "",
        CAURA_AGENT_ID: "agent-a", CAURA_MANUAL_AGENT_API_KEY: "fixture_manual_key", CAURA_MANUAL_AGENT_ID: "agent-a", CAURA_MANUAL_TENANT_ID: "tenant-a",
        CAURA_AUTO_FIX_CONFIG: "false", WIRING_ACTION: action, WIRING_REQUEST: JSON.stringify(request), WIRING_CONFIG: JSON.stringify(config),
        WIRING_DIST: pathToFileURL(DIST).href, BROKER_PLAN: JSON.stringify(plan), BROKER_CACHE: cache,
        BROKER_AUTHORITY: authority, BROKER_TRACE: trace, BROKER_EXPIRY: expiry, ...env}, encoding: "utf8", timeout: 15_000});
    assert.ifError(r.error);
    for (const secret of ["fixture_tenant_key", "fixture_install_key", "fixture_child_key", "fixture_manual_key", "fixture_legacy_key"])
      assert.ok(!(r.stdout + r.stderr).includes(secret), "diagnostics must not expose credentials");
    for (const [name, value] of preserved) assert.equal(readFileSync(join(dir, name), "utf8"), value);
    const line = r.stdout.split("\n").find(s => s.startsWith("WIRING_RESULT:")); assert.ok(line, "runtime must return a bounded result");
    return {exit: r.status, ...JSON.parse(line.slice("WIRING_RESULT:".length))};
  }
  return {run, calls, authority, cache, dir};
}
const resources = (f: ReturnType<typeof fixture>) => f.calls().filter(r => !r.url.includes("/admin/"));
function ok(r: any) {assert.equal(r.ok, true, r.code); assert.equal(r.exit, 0);}
for (const mode of ["", "unknown"]) test(`runtime ${mode || "missing"} mode holds before any outbound call`, () => {
  const f = fixture(), r = f.run({mode}); assert.equal(r.ok, false); assert.match(r.code, /runtime_auth_mode_(required|invalid)/); assert.deepEqual(f.calls(), []);
});
test("absent mode holds before outbound calls", () => {
  const f = fixture(); assert.match(f.run({env: {CAURA_AUTH_MODE: undefined}}).code, /runtime_auth_mode_required/); assert.deepEqual(f.calls(), []);
});
test("manual bootstrap verifies only its configured agent, without an install probe", () => {
  const f = fixture(); ok(f.run({mode: "manual", action: "bootstrap"}));
  assert.equal(f.calls().length, 1); assert.equal(resources(f)[0].headers["x-api-key"], "fixture_manual_key");
});
test("managed runtime uses the bound child, never the legacy cache or tenant key", () => {
  const f = fixture(); ok(f.run()); assert.ok(resources(f).some(r => r.headers["x-api-key"] === "fixture_child_key"));
  assert.ok(f.calls().every(r => r.headers["x-api-key"] !== "fixture_tenant_key"));
});
test("unenrolled managed runtime refuses without a request or identity reset", () => {
  const f = fixture(); rmSync(f.authority); assert.equal(f.run().ok, false); assert.deepEqual(f.calls(), []);
});
for (const mode of ["managed", "manual"]) test(`${mode} resource401 never retries or changes authority`, () => {
  const f = fixture(); assert.equal(f.run({mode, plan: {resourceStatus: 401}}).ok, false);
  const r = resources(f).filter(r => r.url.endsWith("/search")); assert.equal(r.length, 1);
  assert.equal(r[0].headers["x-api-key"], mode === "manual" ? "fixture_manual_key" : "fixture_child_key");
});
for (const status of [401, 403]) test(`manual runtime HTTP${status} holds later calls until restart`, () => {
  const f = fixture(); assert.equal(f.run({mode: "manual", action: "twice", plan: {resourceStatus: status}}).ok, false);
  assert.equal(resources(f).filter(r => r.url.endsWith("/search")).length, 1);
});
for (const manualReply of [{via_gateway: false}, {key_kind: null}, {key_kind: "user_api_key"}]) test(`manual runtime refuses unverified gateway proof ${JSON.stringify(manualReply)}`, () => {
  const f = fixture(); assert.equal(f.run({mode: "manual", plan: {manualReply}}).ok, false);
  assert.ok(!resources(f).some(r => r.url.endsWith("/search")));
});
test("standalone remains an explicit shared/keyless choice without provisioning", () => {
  const f = fixture(); rmSync(f.authority); ok(f.run({mode: "standalone"}));
  assert.equal(resources(f)[0].headers["x-api-key"], "fixture_tenant_key");
  ok(f.run({mode: "standalone", env: {CAURA_API_KEY: ""}}));
  assert.ok(!f.calls().some(r => r.url.includes("/admin/")));
});
for (const header of ["X-API-Key", "X-Tenant-ID", "X-Agent-ID", "X-Caura-Credential-Kind", "Authorization"]) test(`runtime extraHeaders cannot replace ${header}`, () => {
  const f = fixture(); assert.equal(f.run({request: {agentId: "agent-a", extraHeaders: {[header]: "fixture_override"}}}).ok, false);
  assert.ok(!resources(f).some(r => r.url.endsWith("/search")));
});
for (const [action, path] of [["descriptions", "/tool-descriptions"], ["update", "/plugin-manifest"], ["update", "/plugin-source"]]) test(`${path} public fetches are uncredentialed and refuse redirects`, () => {
  const f = fixture(); ok(f.run({action, plan: {update: action === "update"}}));
  const calls = resources(f).filter(r => new URL(r.url).pathname.endsWith(path));
  assert.ok(calls.length >= 1);
  for (const r of calls) {assert.equal(r.headers["x-api-key"], undefined); assert.equal(r.redirect, "error");}
});
test("heartbeat and installation reports use the install principal and preserve diagnostic install_id", () => {
  const f = fixture(); ok(f.run({action: "heartbeat"}));
  const beat = resources(f).find(r => r.url.endsWith("/fleet/heartbeat")); assert.ok(beat);
  assert.equal(beat.headers["x-api-key"], "fixture_install_key"); assert.equal(beat.body.install_id, "abcdef012345");
  ok(f.run({action: "node"})); assert.equal(resources(f).find(r => r.url.endsWith("/skills/installations")).headers["x-api-key"], "fixture_install_key");
});
for (const [tool, params] of [["caura_recall", {query: "fixture query"}], ["caura_manage", {op: "read", memory_id: "fixture-memory"}] ] as const) test(`${tool} uses host-run authority after adapters omit agent_id`, () => {
  const f = fixture(); ok(f.run({action: "tool", request: {tool, params}, plan: {response: {home_agent_id: "agent-b"}, existing: {home_agent_id: "agent-b"}}}));
  assert.ok(f.calls().some(r => r.body?.home_agent_id === "agent-b"));
  assert.ok(resources(f).some(r => r.headers["x-api-key"] === "fixture_child_key"));
});
test("bootstrap probes authenticated identity without creating a synthetic health-check agent or memory", () => {
  const f = fixture(); ok(f.run({action: "bootstrap"}));
  assert.ok(!f.calls().some(r => r.method !== "GET"));
  assert.ok(f.calls().some(r => r.headers["x-api-key"] === "fixture_install_key"));
});
test("configured mode is applied before registration fetches", () => {
  const f = fixture(); ok(f.run({action: "register", mode: "", config: {CAURA_AUTH_MODE: "managed"}}));
  assert.ok(f.calls().some(r => r.url.endsWith("/admin/agent-connect-context")));
  for (const r of resources(f)) assert.equal(r.headers["x-api-key"], undefined);
});
test("legacy issuer implementation and obsolete issuer tests retire together", () => {
  assert.ok(!existsSync(join(SRC, "agent-auth.ts"))); assert.ok(!existsSync(join(SRC, "agent-auth.test.ts")));
});
test("upgrade instructions match the runtime mode-required refusal contract", () => {
  const guide = readFileSync(join(SRC, "../../docs/plugin-upgrade.md"), "utf8");
  for (const term of [/CAURA_AUTH_MODE/, /runtime_auth_mode_required/, /existing/i, /fresh/i, /outbound/i]) assert.match(guide, term);
});

for (const mode of [undefined, "", "unknown"]) test(`bootstrap resolves and logs once for held mode ${mode}`, () => {
  const f = fixture(), r = f.run({action: "bootstrap", env: {CAURA_AUTH_MODE: mode}}); ok(r); assert.deepEqual(f.calls(), []);
  assert.equal(r.diagnostics.filter((s: string) => /runtime_auth_mode_(required|invalid)/.test(s)).length, 1);
});
test("manual assemble skips automatic requests and preserves backend reachability", () => {
  const f = fixture(), r = f.run({action: "assemble", mode: "manual"}); ok(r); assert.deepEqual(r.diagnostics, []);
  assert.equal(f.calls().length, 1); assert.ok(f.calls()[0].url.endsWith("/whoami"));
});

for (const publicPath of ["/tool-descriptions", "/plugin-manifest", "/plugin-source"]) for (const status of [401, 403])
  test(`manual agent remains usable after public ${publicPath} HTTP${status}`, () => {
    const f = fixture(); ok(f.run({mode: "manual", action: "public-then-agent", request: {agentId: "agent-a", publicPath}, plan: {publicStatus: status}}));
    const publicCall = resources(f).find(r => r.url.endsWith(publicPath)); assert.ok(publicCall); assert.equal(publicCall.headers["x-api-key"], undefined);
    assert.equal(resources(f).find(r => r.url.endsWith("/search")).headers["x-api-key"], "fixture_manual_key");
  });

test("a missing manifest logs the hardcoded-list fallback", () => {
  const f = fixture(), r = f.run({action: "update", plan: {update: true, manifestStatus: 404}}); ok(r);
  assert.ok(f.calls().some(c => c.url.endsWith("/plugin-manifest")));
  assert.ok(f.calls().some(c => c.url.includes("plugin-source?file=env.ts")));
  assert.ok(r.diagnostics.some((s: string) => /plugin-manifest returned 404; using fallback file list/.test(s)));
});

for (const suppliedGuard of [false, true]) for (const mode of [undefined, "", "manual"]) test(`child options survive guard=${suppliedGuard} mode=${mode}`, () => {
  const option = `--import=${pathToFileURL(join(DIST, "runtime-fetch-guard.fixture.js")).href}`;
  const r = spawnSync(process.execPath, ["--input-type=module", "--eval", 'console.log(JSON.stringify({limit:Error.stackTraceLimit,options:process.env.NODE_OPTIONS,mode:process.env.CAURA_AUTH_MODE}))'], {
    env: {PATH:process.env.PATH,HOME:fixture().dir,CAURA_AUTH_MODE:mode,NODE_OPTIONS:`--stack-trace-limit=77${suppliedGuard ? " " + option : ""}`},encoding:"utf8",timeout:5000});
  assert.equal(r.status,0,r.stderr); const child = JSON.parse(r.stdout);
  assert.equal(child.limit,77); assert.equal(child.mode,mode); assert.equal(child.options.split(option).length-1,1);
});
