/** Additive broker contract. Runtime activation belongs to a later change. */
import {afterEach, test} from "node:test";
import assert from "node:assert/strict";
import {spawn, spawnSync} from "node:child_process";
import {chmodSync, existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync,
  rmSync, statSync, symlinkSync, writeFileSync} from "node:fs";
import {hostname, tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {BROKER_PRELOAD, BROKER_RUNNER} from "./install-agent-auth.fixture.js";
import {FROZEN_PLUGIN_ID} from "./legacy-contracts.fixture.js";

const DIST = dirname(fileURLToPath(import.meta.url)), SRC = join(DIST, "../src");
const INSTALL = "11111111-1111-4111-8111-111111111111";
const ORIGIN = "https://broker.example.test", BASE = "/api/v1/admin/orgs/org-a/api-credentials";
const options = {gateway: ORIGIN, apiPrefix: "/api/v1", orgId: "org-a", tenantId: "tenant-a",
  installUuid: INSTALL, agentId: "agent-a", capabilities: ["read", "write"]};
const dirs: string[] = [];
afterEach(() => {for (const dir of dirs.splice(0)) rmSync(dir, {recursive: true, force: true});});

function fixture() {
  const home = mkdtempSync(join(tmpdir(), "install-broker-")); dirs.push(home);
  const dir = join(home, ".openclaw/plugins", FROZEN_PLUGIN_ID);
  mkdirSync(dir, {recursive: true, mode: 0o700});
  const authority = join(dir, ".installation-authority.json"), cache = join(dir, ".installation-agent-keys.json");
  const trace = join(home, "trace.jsonl"), preload = join(home, "preload.mjs");
  const expiry = new Date(Date.now() + 30 * 86400_000).toISOString();
  const journal = {schema_version: 1, status: "verified", mode: "register", gateway: ORIGIN,
    api_prefix: "/api/v1", hostname: hostname(), platform: process.platform, arch: process.arch,
    created_at: new Date().toISOString(), expected_tenant: "tenant-a", expected_fleet: "fleet-a",
    install_uuid: INSTALL, machine_fingerprint: "a".repeat(64), install_credential: "fixture_install_key",
    tenant_id: "tenant-a", org_id: "org-a", fleet_id: "fleet-a", expires_at: expiry};
  writeFileSync(authority, JSON.stringify(journal), {mode: 0o600});
  writeFileSync(preload, BROKER_PRELOAD);
  const legacy = new Map([[".agent-keys.json", '{"version":1,"keys":{"agent-a":{"key":"fixture_legacy_key"}}}'],
    ["install.json", '{"install_id":"legacy-diagnostic-id","default_agent_id":"main"}'],
    [".env", "CAURA_API_KEY=fixture_tenant_key\nCAURA_FLEET_ID=untrusted-fleet\n"]]);
  for (const [name, value] of legacy) writeFileSync(join(dir, name), value, {mode: 0o600});
  const read = () => existsSync(cache) ? JSON.parse(readFileSync(cache, "utf8")) : null;
  const requests = () => existsSync(trace) ? readFileSync(trace, "utf8").trim().split("\n").filter(Boolean).map(s => JSON.parse(s)) : [];
  const mutations = () => requests().filter(r => r.method === "POST");
  function env(plan = {}, action = "resolve", override = {}) {
    return {PATH: process.env.PATH, HOME: home, NODE_ENV: "test", CAURA_API_KEY: "fixture_tenant_key",
      BROKER_PLAN: JSON.stringify(plan), BROKER_OPTIONS: JSON.stringify({...options, ...override}),
      BROKER_ACTION: action, BROKER_CACHE: cache, BROKER_TRACE: trace, BROKER_EXPIRY: expiry,
      BROKER_MODULE: pathToFileURL(join(DIST, "install-agent-auth.js")).href};
  }
  const args = ["--import", pathToFileURL(preload).href, "--input-type=module", "--eval", BROKER_RUNNER];
  function decode(stdout: string, stderr: string) {
    assert.ok(stdout.length + stderr.length < 2048, "diagnostics stay bounded");
    for (const value of ["fixture_install_key", "fixture_child_key", "fixture_rotated_key", "fixture_tenant_key", "fixture_server_body"])
      assert.ok(!(stdout + stderr).includes(value), "bounded diagnostics must not expose credentials or server bodies");
    for (const [name, value] of legacy) assert.equal(readFileSync(join(dir, name), "utf8"), value);
    return stdout.trim() ? JSON.parse(stdout) : null;
  }
  function run(plan = {}, action = "resolve", override = {}) {
    const r = spawnSync(process.execPath, args, {env: env(plan, action, override), encoding: "utf8", timeout: 10_000});
    assert.ifError(r.error); return {status: r.status, result: decode(r.stdout, r.stderr)};
  }
  return {home, dir, authority, cache, expiry, journal, read, requests, mutations, run,
    saveAuthority(patch: object) {writeFileSync(authority, JSON.stringify({...journal, ...patch}));},
    saveCache(value: unknown) {writeFileSync(cache, JSON.stringify(value), {mode: 0o600});},
    start(plan: object) {
      const child = spawn(process.execPath, args, {env: env(plan), stdio: ["ignore", "pipe", "pipe", "ipc"]});
      let stdout = "", stderr = "";
      assert.ok(child.stdout && child.stderr);
      child.stdout.on("data", b => {stdout += b;}); child.stderr.on("data", b => {stderr += b;});
      const started = new Promise<void>(resolve => {child.once("message", () => resolve()); child.once("exit", () => resolve());});
      const done = new Promise<ReturnType<typeof run>>(resolve => child.once("close", status => resolve({status, result: decode(stdout, stderr)})));
      return {child, started, done};
    }};
}
function ok(r: ReturnType<ReturnType<typeof fixture>["run"]>) {
  assert.equal(r.status, 0); assert.equal(r.result?.ok, true, r.result?.code);
  assert.equal(r.result.matched, true);
}
function refused(r: ReturnType<ReturnType<typeof fixture>["run"]>) {
  assert.equal(r.status, 0); assert.equal(r.result?.ok, false);
  assert.match(r.result.code, /^install_agent_[a-z0-9_]+$/);
  assert.notEqual(r.result.code, "install_agent_unavailable");
}

test("canonical issuance binds verified installation, persists pending before mutation and publishes before returning", () => {
  const f = fixture(); ok(f.run()); const [write] = f.mutations();
  assert.equal(write.url, ORIGIN + BASE); assert.equal(f.mutations().length, 1);
  assert.equal(write.before.entries["agent-a"].status, "pending");
  assert.match(write.before.entries["agent-a"].operation.id, /^[0-9a-f-]{36}$/);
  assert.equal(write.before.entries["agent-a"].operation.kind, "create");
  assert.equal(write.body.kind, "agent_key"); assert.equal(write.body.home_agent_id, "agent-a");
  assert.equal(write.body.home_tenant_id, "tenant-a"); assert.deepEqual(write.body.capabilities, ["read", "write"]);
  assert.deepEqual(write.body.source_tenant_ids, []); assert.equal(write.body.read_all_org_tenants, false);
  for (const field of ["agent_id", "install_id", "install_uuid", "issuing_install_uuid", "trust_level", "fleet_id", "owner_user_id"])
    assert.equal(write.body[field], undefined, `${field} is not caller authority`);
  assert.notEqual(write.body.bootstrap_allowed, true); assert.ok(!write.body.expires_at, "server owns default expiry");
  for (const request of f.requests()) {
    assert.equal(request.headers["x-api-key"], "fixture_install_key");
    for (const h of ["x-gateway-secret", "x-install-uuid", "x-agent-id", "x-tenant-id"]) assert.equal(request.headers[h], undefined);
    assert.equal(request.redirect, "error"); assert.ok(request.bounded > 0 && request.bounded <= 10_000);
  }
  assert.equal(f.requests()[0].url, ORIGIN + "/api/v1/admin/agent-connect-context");
  assert.ok(f.requests().some(r => r.method === "GET" && new URL(r.url).pathname === BASE));
  const cache = f.read(); assert.equal(cache.schema_version, 1);
  assert.deepEqual(cache.binding, {gateway: ORIGIN, api_prefix: "/api/v1", org_id: "org-a", tenant_id: "tenant-a", install_uuid: INSTALL, hostname: hostname()});
  assert.equal(cache.entries["agent-a"].status, "ready");
  assert.equal(cache.entries["agent-a"].key, "fixture_child_key");
  assert.equal(cache.entries["agent-a"].credential.issuing_install_uuid, INSTALL);
  assert.equal(cache.entries["agent-a"].credential.expires_at, f.expiry);
  assert.equal(statSync(f.cache).mode & 0o777, 0o600);
});

test("warm resolution revalidates parent context and exact owned metadata instead of trusting disk", () => {
  const f = fixture(); ok(f.run()); const n = f.requests().length; ok(f.run());
  const recent = f.requests().slice(n); assert.ok(recent[0].url.endsWith("/agent-connect-context"));
  assert.ok(recent.some(r => r.url.endsWith("/22222222-2222-4222-8222-222222222222")));
  assert.equal(f.mutations().length, 1);
});
test("missing enrollment cannot discover legacy, tenant or bootstrap authority", () => {
  const f = fixture(); rmSync(f.authority); refused(f.run()); assert.deepEqual(f.requests(), []);
});
for (const patch of [{status: "held"}, {status: "credential_saved"}, {fleet_id: undefined},
  {expires_at: "2000-01-01T00:00:00Z"}, {hostname: "another-host"}]) {
  test(`unusable installation ${JSON.stringify(patch)} refuses before credential traffic`, () => {
    const f = fixture(); f.saveAuthority(patch); refused(f.run()); assert.deepEqual(f.requests(), []);
  });
}
for (const patch of [{gateway: "https://other.example.test"}, {tenantId: "other"}, {orgId: "other"},
  {installUuid: "44444444-4444-4444-8444-444444444444"}, {apiPrefix: "/other"}, {capabilities: ["admin"]}]) {
  test(`caller scope ${Object.keys(patch)[0]} cannot substitute authority`, () => {
    const f = fixture(); refused(f.run({}, "resolve", patch)); assert.deepEqual(f.requests(), []);
  });
}
for (const [field, value] of [["gateway", "https://other.example.test"], ["org_id", "other"],
  ["tenant_id", "other"], ["install_uuid", "44444444-4444-4444-8444-444444444444"], ["hostname", "other"]]) {
  test(`cache binding ${field} drift never adopts or replaces a legacy entry`, () => {
    const f = fixture(); ok(f.run()); const cache = f.read(); cache.binding[field] = value; f.saveCache(cache);
    refused(f.run()); assert.equal(f.mutations().length, 1);
  });
}
for (const patch of [{tenant_id: "other"}, {org_id: "other"}, {install_uuid: "44444444-4444-4444-8444-444444444444"}, {hostname: "other"}]) {
  test(`fresh context ${Object.keys(patch)[0]} must match journal before issuance`, () => {
    const f = fixture(); refused(f.run({context: patch})); assert.equal(f.mutations().length, 0);
  });
}
for (const patch of [{raw_key: ""}, {id: ""}, {kind: "user_api_key"}, {org_id: "other"},
  {home_tenant_id: "other"}, {home_agent_id: "other"}, {issuing_install_uuid: null},
  {capabilities: ["read", "write", "admin"]}, {source_tenant_ids: ["other"]}, {read_all_org_tenants: true},
  {bootstrap_allowed: true}, {revoked_at: "2026-01-01T00:00:00Z"}, {expires_at: null},
  {expires_at: "2000-01-01T00:00:00Z"}, {expires_at: "invalid"}, {expires_at: "2099-01-01T00:00:00Z"}]) {
  test(`mutation response ${JSON.stringify(patch)} never publishes an unbound key or remints on restart`, () => {
    const f = fixture(); refused(f.run({response: patch})); refused(f.run()); assert.equal(f.mutations().length, 1);
  });
}
for (const kind of ["malformed", "schema", "permissions", "symlink", "hardlink", "lock"] as const) {
  test(`unsafe cache ${kind} is not reset or adopted`, () => {
    const f = fixture(), target = join(f.home, "unchanged.json"); writeFileSync(target, "{}", {mode: 0o600});
    if (kind === "lock") writeFileSync(f.cache + ".lock", "operator recovery", {mode: 0o600});
    else if (kind === "symlink") symlinkSync(target, f.cache);
    else if (kind === "hardlink") linkSync(target, f.cache);
    else {writeFileSync(f.cache, kind === "malformed" ? "{" : '{"schema_version":99}', {mode: 0o600}); if (kind === "permissions") chmodSync(f.cache, 0o644);}
    const path = kind === "lock" ? f.cache + ".lock" : f.cache, before = readFileSync(path, "utf8");
    refused(f.run()); assert.equal(readFileSync(path, "utf8"), before); assert.equal(f.mutations().length, 0);
    assert.equal(readFileSync(target, "utf8"), "{}");
  });
}
for (const plan of [{failPendingSave: true}, {failReadySave: true}, {lostResponse: true}, {invalidJson: true}, {hugeResponse: true}]) {
  test(`persistence/response failure ${Object.keys(plan)[0]} cannot return memory-only keys`, () => {
    const f = fixture(); refused(f.run(plan)); refused(f.run(plan));
    assert.equal(f.mutations().length, "failPendingSave" in plan ? 0 : 1);
  });
}
for (const status of [401, 403, 404, 409, 429, 500]) {
  test(`canonical HTTP ${status} cannot fall back, change identity or replay a mutation`, () => {
    const f = fixture(); refused(f.run({mutationStatus: status})); refused(f.run()); assert.equal(f.mutations().length, 1);
  });
  test(`parent context HTTP ${status} cannot mint or use a cached child`, () => {
    const f = fixture(); ok(f.run()); const n = f.mutations().length;
    refused(f.run({contextStatus: status})); assert.equal(f.mutations().length, n);
  });
  test(`owned catalog HTTP ${status} cannot be treated as an empty credential set`, () => {
    const f = fixture(); refused(f.run({listStatus: status})); assert.equal(f.mutations().length, 0);
  });
}
test("ambiguous committed create stays held after crash and explicit lock recovery even with zero catalog matches", () => {
  const f = fixture(); assert.equal(f.run({crashAfterMutation: true}).status, 71);
  assert.ok(existsSync(f.cache + ".lock")); refused(f.run());
  rmSync(f.cache + ".lock"); // Simulated explicit operator recovery, never broker behavior.
  refused(f.run({catalog: []})); assert.equal(f.mutations().length, 1);
});
for (const count of [1, 2]) {
  test(`${count} catalog matches without a recoverable raw key never authorize a new create`, () => {
    const f = fixture(); refused(f.run({catalog: Array.from({length: count}, () => ({
      id: "22222222-2222-4222-8222-222222222222", kind: "agent_key", home_agent_id: "agent-a",
      home_tenant_id: "tenant-a", org_id: "org-a", issuing_install_uuid: INSTALL,
      source_tenant_ids: [], read_all_org_tenants: false, capabilities: ["read", "write"],
      expires_at: f.expiry, revoked_at: null,
    }))})); assert.equal(f.mutations().length, 0);
  });
}
test("same-process concurrent resolution coalesces into one durable issuance", () => {
  const f = fixture(); ok(f.run({}, "coalesce")); assert.equal(f.mutations().length, 1);
});
test("another process cannot issue while the pending operation owns the lock", {timeout: 15_000}, async () => {
  const f = fixture(), first = f.start({pauseMutation: true});
  try {await first.started; refused(f.run()); assert.equal(f.mutations().length, 1);
    first.child.send("release"); ok(await first.done);
  } finally {first.child.kill();}
});
test("owned rotation targets the cached credential and preserves bounded expiry and provenance", () => {
  const f = fixture(); ok(f.run()); ok(f.run({}, "rotate"));
  const [create, rotate] = f.mutations(); assert.equal(rotate.url, create.url + "/22222222-2222-4222-8222-222222222222/rotate");
  assert.equal(rotate.before.entries["agent-a"].operation.kind, "rotate");
  assert.equal(rotate.body.expires_at, undefined); assert.equal(f.read().entries["agent-a"].credential.expires_at, f.expiry);
});
for (const existing of [{issuing_install_uuid: "44444444-4444-4444-8444-444444444444"},
  {home_agent_id: "other"}, {revoked_at: "2026-01-01T00:00:00Z"}, {expires_at: "2000-01-01T00:00:00Z"}]) {
  test(`warm metadata ${Object.keys(existing)[0]} refuses both reuse and rotation without a remint`, () => {
    const f = fixture(); ok(f.run()); refused(f.run({existing})); refused(f.run({existing}, "rotate"));
    assert.equal(f.mutations().length, 1);
  });
}
for (const status of [401, 403, 404, 409, 500]) {
  test(`owned metadata HTTP ${status} cannot return a cached key or cause a replacement`, () => {
    const f = fixture(); ok(f.run()); refused(f.run({getStatus: status}));
    refused(f.run({getStatus: status}, "rotate")); assert.equal(f.mutations().length, 1);
  });
}
test("failed rotation acknowledgement never silently creates or rotates again", () => {
  const f = fixture(); ok(f.run()); refused(f.run({lostResponse: true}, "rotate"));
  refused(f.run()); refused(f.run({}, "rotate")); assert.equal(f.mutations().length, 2);
});
for (const response of [{rotated_from_id: "another-credential"}, {id: "22222222-2222-4222-8222-222222222222"}]) {
  test(`rotation successor ${Object.keys(response)[0]} must identify a new child of the owned target`, () => {
    const f = fixture(); ok(f.run()); refused(f.run({response}, "rotate")); refused(f.run());
    assert.equal(f.mutations().length, 2);
  });
}
test("explicit renewal uses canonical creation and persists its actual new expiry", () => {
  const f = fixture(); ok(f.run()); const cache = f.read();
  cache.entries["agent-a"].credential.expires_at = "2000-01-01T00:00:00Z"; f.saveCache(cache);
  ok(f.run({existing: {expires_at: "2000-01-01T00:00:00Z"}}, "renew"));
  assert.equal(f.mutations().length, 2); assert.ok(f.mutations().every(r => r.url === ORIGIN + BASE));
  assert.equal(f.read().entries["agent-a"].credential.expires_at, f.expiry);
});
test("broker import is inert and transitive runtime graphs cannot consume it or env.ts", () => {
  const f = fixture(); const r = f.run({}, "import"); assert.equal(r.result?.ok, true);
  assert.deepEqual(f.requests(), []); assert.equal(f.read(), null);
  const visited = new Set<string>();
  function inspect(name: string, runtime: boolean) {
    const tag = `${runtime}:${name}`; if (visited.has(tag)) return; visited.add(tag);
    if (runtime) assert.notEqual(name, "install-agent-auth.ts", "B is not runtime activation");
    else assert.notEqual(name, "env.ts", "broker must not discover tenant secrets");
    const source = readFileSync(join(SRC, name), "utf8");
    if (runtime && name !== "paths.ts") {
      assert.ok(!source.includes(".installation-agent-keys.json"));
      assert.ok(!source.includes("getInstallAgentKeysPath"));
    }
    for (const m of source.matchAll(/(?:from\s+|import\s*\()\s*["']\.\/([^"']+)\.js["']/g)) {
      const child = `${m[1]}.ts`; if (existsSync(join(SRC, child))) inspect(child, runtime);
    }
  }
  for (const entry of ["index.ts", "transport.ts", "agent-auth.ts", "heartbeat.ts"]) inspect(entry, true);
  inspect("install-agent-auth.ts", false); assert.ok(visited.size > 10);
});
