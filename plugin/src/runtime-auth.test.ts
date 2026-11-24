/** Explicit request-authority contract; no runtime activation in this change. */
import {afterEach, test} from "node:test";
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {hostname, tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {AUTH_PRELOAD, AUTH_RUNNER} from "./runtime-auth.fixture.js";
import {FROZEN_PLUGIN_ID} from "./legacy-contracts.fixture.js";

const DIST = dirname(fileURLToPath(import.meta.url)), SRC = join(DIST, "../src");
const ORIGIN = "https://broker.example.test", INSTALL = "11111111-1111-4111-8111-111111111111";
const SETTINGS = {mode: "CAURA_AUTH_MODE", manualKey: "CAURA_MANUAL_AGENT_API_KEY",
  manualAgentId: "CAURA_MANUAL_AGENT_ID", manualTenantId: "CAURA_MANUAL_TENANT_ID"};
const dirs: string[] = [];
afterEach(() => {for (const p of dirs.splice(0)) rmSync(p, {recursive: true, force: true});});
function fixture() {
  const home = mkdtempSync(join(tmpdir(), "runtime-authority-")); dirs.push(home);
  const dir = join(home, ".openclaw/plugins", FROZEN_PLUGIN_ID); mkdirSync(dir, {recursive: true, mode: 0o700});
  const journal = join(dir, ".installation-authority.json"), cache = join(dir, ".installation-agent-keys.json");
  const trace = join(home, "trace.jsonl"), preload = join(home, "preload.mjs");
  const expiry = new Date(Date.now() + 30 * 86400_000).toISOString();
  const authority = {schema_version: 1, status: "verified", mode: "register", gateway: ORIGIN,
    api_prefix: "/api/v1", hostname: hostname(), platform: process.platform, arch: process.arch,
    created_at: new Date().toISOString(), expected_tenant: "tenant-a", expected_fleet: "fleet-a",
    install_uuid: INSTALL, machine_fingerprint: "a".repeat(64), install_credential: "fixture_install_key",
    tenant_id: "tenant-a", org_id: "org-a", fleet_id: "fleet-a", expires_at: expiry};
  writeFileSync(journal, JSON.stringify(authority), {mode: 0o600}); writeFileSync(preload, AUTH_PRELOAD);
  const legacy = join(dir, ".agent-keys.json"), legacyBytes = '{"version":1,"keys":{"agent-a":{"key":"fixture_legacy_key"}}}';
  writeFileSync(legacy, legacyBytes, {mode: 0o600});
  function run({config = {}, request = {}, plan = {}, action = "authorize", expected = "agent", status = 401} = {}) {
    const c = {mode: "managed", gateway: ORIGIN, apiPrefix: "/api/v1", tenantId: "tenant-a", fleetId: "fleet-a",
      standaloneKey: "fixture_tenant_key", manualKey: "fixture_manual_key", manualAgentId: "agent-a", manualTenantId: "tenant-a", ...config};
    const r = spawnSync(process.execPath, ["--import", pathToFileURL(preload).href, "--input-type=module", "--eval", AUTH_RUNNER], {
      env: {PATH: process.env.PATH, HOME: home, NODE_ENV: "test", CAURA_API_KEY: "fixture_tenant_key",
        CAURA_AUTH_MODE: "standalone", CAURA_MANUAL_AGENT_API_KEY: "fixture_environment_manual_key",
        AUTH_CONFIG: JSON.stringify(c), AUTH_REQUEST: JSON.stringify({authority: "agent", method: "POST", path: "/search", agentId: "agent-a", ...request}),
        AUTH_ACTION: action, AUTH_STATUS: String(status), AUTH_EXPECTED: expected,
        AUTH_MODULE: pathToFileURL(join(DIST, "runtime-auth.js")).href, BROKER_PLAN: JSON.stringify(plan),
        BROKER_CACHE: cache, BROKER_AUTHORITY: journal, BROKER_TRACE: trace, BROKER_EXPIRY: expiry},
      encoding: "utf8", timeout: 15_000});
    assert.ifError(r.error); assert.equal(r.status, 0); assert.ok(r.stdout.length + r.stderr.length < 2048);
    for (const secret of ["fixture_tenant_key", "fixture_install_key", "fixture_child_key", "fixture_manual_key", "fixture_environment_manual_key", "fixture_legacy_key", "fixture_server_body"])
      assert.ok(!(r.stdout + r.stderr).includes(secret), "no credential or server-body diagnostics");
    assert.equal(readFileSync(legacy, "utf8"), legacyBytes);
    return JSON.parse(r.stdout.trim());
  }
  const requests = () => existsSync(trace) ? readFileSync(trace, "utf8").trim().split("\n").filter(Boolean).map(s => JSON.parse(s)) : [];
  return {run, requests, journal, cache, authority,
    saveAuthority(patch: object) {writeFileSync(journal, JSON.stringify({...authority, ...patch}));}};
}
function ok(r: any) {assert.equal(r.ok, true, r.code); assert.equal(r.matched, true); assert.deepEqual(r.identityHeaders, []);}
function held(r: any) {assert.equal(r.ok, false); assert.match(r.code, /^runtime_auth_[a-z0-9_]+$/); assert.notEqual(r.code, "runtime_auth_unavailable");}

test("setting names are fixed before remote guards and runtime adoption", () => {
  const f = fixture(), r = f.run({action: "settings"}); assert.equal(r.ok, true); assert.deepEqual(r.settings, SETTINGS);
});
for (const mode of [undefined, "", " ", "unknown", "MANAGED", false]) {
  test(`mode ${JSON.stringify(mode)} holds without network or credential discovery`, () => {
    const f = fixture(), r = f.run({config: {mode}}); held(r);
    assert.equal(r.code, mode === undefined || (typeof mode === "string" && !mode.trim()) ?
      "runtime_auth_mode_required" : "runtime_auth_mode_invalid");
    assert.deepEqual(f.requests(), []); assert.ok(!existsSync(f.cache));
  });
}
test("managed agent resolution uses canonical bound delegation, never a retained tenant or legacy key", () => {
  const f = fixture(); ok(f.run()); const calls = f.requests();
  assert.ok(calls.some(r => r.url.includes("/api-credentials") && r.method === "POST"));
  assert.ok(calls.every(r => r.headers["x-api-key"] === "fixture_install_key"));
  assert.ok(calls.every(r => !r.url.includes("/agent-keys/provision")));
});
for (const patch of [{status: "held"}, {fleet_id: undefined}, {hostname: "other"}, {expires_at: "2000-01-01T00:00:00Z"}]) {
  test(`managed unusable journal ${Object.keys(patch)[0]} cannot become another mode`, () => {
    const f = fixture(); f.saveAuthority(patch); held(f.run()); assert.deepEqual(f.requests(), []);
  });
}
test("managed missing journal or enrollment lock cannot discover another authority", () => {
  const f = fixture(); rmSync(f.journal); held(f.run()); assert.deepEqual(f.requests(), []);
  f.saveAuthority({}); writeFileSync(f.journal + ".lock", "fixture operator", {mode: 0o600});
  held(f.run()); assert.deepEqual(f.requests(), []);
});
for (const config of [{gateway: "https://other.example.test"}, {tenantId: "other"}, {fleetId: "other"}, {apiPrefix: "/other"}]) {
  test(`configured managed ${Object.keys(config)[0]} drift holds before credential use`, () => {
    const f = fixture(); held(f.run({config})); assert.deepEqual(f.requests(), []);
  });
}
for (const plan of [{contextStatus: 404}, {contextStatus: 401}, {contextStatus: 500}, {listStatus: 403},
  {context: {org_id: "other"}}, {context: {tenant_id: "other"}}, {context: {install_uuid: "44444444-4444-4444-8444-444444444444"}},
  {context: {hostname: "other"}}, {mutationStatus: 409}, {mutationStatus: 429}, {lostResponse: true}, {response: {issuing_install_uuid: null}}]) {
  test(`managed failure ${JSON.stringify(plan)} never falls back to shared/manual/anonymous access`, () => {
    const f = fixture(); held(f.run({plan}));
    assert.ok(f.requests().every(r => r.headers["x-api-key"] === "fixture_install_key"));
    assert.ok(f.requests().every(r => !r.url.includes("/agent-keys/provision")));
  });
}
test("a revoked managed child cannot be reminted or replaced with a broader key", () => {
  const f = fixture(); ok(f.run()); const before = readFileSync(f.cache, "utf8");
  held(f.run({plan: {existing: {revoked_at: "2026-01-01T00:00:00Z"}}}));
  assert.equal(readFileSync(f.cache, "utf8"), before);
  assert.equal(f.requests().filter(r => r.method === "POST").length, 1);
});
for (const request of [{agentId: undefined}, {agentId: "", caller_agent_id: "agent-a"}, {authority: undefined},
  {authority: "install"}, {authority: "none"}]) {
  test(`agent data refuses missing or wrong authority ${JSON.stringify(request)}`, () => {
    const f = fixture(); held(f.run({request})); assert.ok(!f.requests().some(r => r.method === "POST"));
  });
}
for (const [method, path] of [["POST", "/fleet/heartbeat"], ["POST", "/fleet/commands/22222222-2222-4222-8222-222222222222/result"],
  ["POST", "/skills/installable"], ["POST", "/skills/installations"], ["GET", "/whoami"]]) {
  test(`managed node authority is explicit for ${method} ${path}`, () => {
    const f = fixture(); ok(f.run({request: {authority: "install", method, path, agentId: undefined}, expected: "install"}));
    assert.ok(!f.requests().some(r => r.method === "POST"));
  });
}
for (const request of [{authority: "install", path: "/documents"}, {authority: "install", path: "/admin/settings"},
  {authority: "install", method: "GET", path: "/fleet/heartbeat"}, {authority: "agent", path: "/skills/installations"}]) {
  test(`install allowlist refuses ${JSON.stringify(request)}`, () => held(fixture().run({request})));
}
// These exact gateway locations omit auth_request: nginx.conf.template:1801,1939,2039.
for (const path of ["/plugin-source", "/plugin-manifest", "/tool-descriptions"]) {
  test(`public ${path} gets explicit none, never an installation credential`, () => {
    const f = fixture(); ok(f.run({request: {authority: "none", method: "GET", path}, expected: "none"}));
    held(f.run({request: {authority: "install", method: "GET", path}}));
    held(f.run({request: {authority: "agent", method: "GET", path}}));
  });
}
test("standalone shared/keyless behavior requires an explicit mode and never provisions", () => {
  const f = fixture(); rmSync(f.journal);
  ok(f.run({config: {mode: "standalone"}, expected: "standalone"}));
  ok(f.run({config: {mode: "standalone", standaloneKey: ""}, expected: "none"})); assert.deepEqual(f.requests(), []);
});
// Gateway kind is the stored row kind: routers/auth.py:7285,7409, agent_key at :5938.
test("manual startup probe sends only its dedicated key and verifies gateway identity", () => {
  const f = fixture(); rmSync(f.journal); ok(f.run({config: {mode: "manual"}, expected: "manual"}));
  const [probe] = f.requests(); assert.equal(f.requests().length, 1); assert.ok(probe.url.endsWith("/whoami"));
  assert.equal(probe.headers["x-api-key"], "fixture_manual_key"); assert.equal(probe.redirect, "error"); assert.ok(probe.bounded > 0);
  for (const header of ["x-tenant-id", "x-agent-id", "x-caura-credential-kind", "x-install-uuid", "x-gateway-secret", "authorization"])
    assert.equal(probe.headers[header], undefined);
});
for (const plan of [{manualReply: {via_gateway: false}}, {omitManual: ["key_kind"]}, {manualReply: {key_kind: "user_api_key"}},
  {manualReply: {key_kind: "install_credential"}}, {manualReply: {auth_source: "standalone"}},
  {manualReply: {tenant_id: "other"}}, {manualReply: {agent_id: "other"}}, {whoamiStatus: 401}, {whoamiStatus: 403}]) {
  test(`manual proof failure ${JSON.stringify(plan)} holds without another authority`, () => {
    const f = fixture(); held(f.run({config: {mode: "manual"}, plan}));
    assert.ok(f.requests().every(r => r.headers["x-api-key"] === "fixture_manual_key")); assert.ok(!existsSync(f.cache));
  });
}
for (const config of [{manualKey: ""}, {manualAgentId: ""}, {manualTenantId: ""}]) {
  test(`manual ${Object.keys(config)[0]} cannot fall back to tenant credentials or local mappings`, () => {
    const f = fixture(); held(f.run({config: {mode: "manual", ...config}})); assert.deepEqual(f.requests(), []);
  });
}
test("manual cannot select another agent or perform node operations", () => {
  const f = fixture(); held(f.run({config: {mode: "manual"}, request: {agentId: "other"}}));
  held(f.run({config: {mode: "manual"}, request: {authority: "install", path: "/fleet/heartbeat"}}));
});
for (const status of [401, 403]) {
  for (const action of ["hold", "reinitialize"]) {
    test(`manual HTTP ${status} holds across ${action} without re-probing or selecting another credential`, () => {
      const f = fixture(); held(f.run({config: {mode: "manual"}, status, action})); assert.equal(f.requests().length, 1);
    });
  }
}
test("a newly constructed manual client can re-probe only after an explicit restart", () => {
  const f = fixture(); ok(f.run({config: {mode: "manual"}, action: "restart", expected: "manual"})); assert.equal(f.requests().length, 2);
});
for (const header of ["X-API-Key", "x-api-key", "X-Tenant-ID", "X-Agent-ID", "X-Caura-Credential-Kind", "X-Install-UUID", "X-Gateway-Secret", "Authorization"]) {
  test(`extraHeaders cannot override credential/identity header ${header}`, () => {
    const f = fixture(); held(f.run({request: {extraHeaders: {[header]: "fixture_override"}}}));
    assert.ok(!f.requests().some(r => r.method === "POST"));
  });
}
test("ordinary per-attempt headers survive authority selection", () => {
  const r = fixture().run({request: {extraHeaders: {"X-Bulk-Attempt-Id": "attempt-a"}}}); ok(r); assert.equal(r.harmlessHeader, "attempt-a");
});
test("selector construction is inert; runtime cannot import the operator CLI", () => {
  const f = fixture(), r = f.run({action: "import"}); assert.equal(r.ok, true); assert.deepEqual(f.requests(), []); assert.ok(!existsSync(f.cache));
  const seen = new Set<string>();
  function inspect(name: string, runtime: boolean) {
    const tag = runtime + name; if (seen.has(tag)) return; seen.add(tag);
    if (runtime) assert.notEqual(name, "enroll-installation.ts"); else assert.notEqual(name, "env.ts");
    const source = readFileSync(join(SRC, name), "utf8");
    for (const m of source.matchAll(/(?:from\s+|import\s*\()\s*["']\.\/([^"']+)\.js["']/g)) {
      const child = m[1] + ".ts"; if (existsSync(join(SRC, child))) inspect(child, runtime);
    }
  }
  for (const root of ["index.ts", "transport.ts", "heartbeat.ts"]) inspect(root, true);
  inspect("runtime-auth.ts", false);
});
