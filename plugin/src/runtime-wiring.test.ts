/** Integration boundary for the complete runtime switch, never a partial downgrade. */
import {afterEach, test} from "node:test";
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync} from "node:fs";
import {hostname, tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {FROZEN_PLUGIN_ID} from "./legacy-contracts.fixture.js";
import {WIRING_PRELOAD, WIRING_RUNNER} from "./runtime-wiring.fixture.js";
const DIST = dirname(fileURLToPath(import.meta.url)), SRC = join(DIST, "../src"), ORIGIN = "https://broker.example.test";
const dirs: string[] = [];
afterEach(() => {for (const p of dirs.splice(0)) rmSync(p, {recursive: true, force: true});});
function fixture() {
  const home = realpathSync(mkdtempSync(join(tmpdir(), "runtime-wiring-"))); dirs.push(home);
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
  function readyCache(credentialExpiry = expiry) {
    const entries = Object.fromEntries(["agent-a", "agent-b"].map((agent, i) => [agent, {status: "ready",
      operation: {id: "44444444-4444-4444-8444-444444444444", kind: "create"}, key: "fixture_child_key",
      credential: {id: i ? "33333333-3333-4333-8333-333333333333" : "22222222-2222-4222-8222-222222222222",
        org_id: "org-a", kind: "agent_key", home_tenant_id: "tenant-a", home_agent_id: agent,
        issuing_install_uuid: journal.install_uuid, source_tenant_ids: [], read_all_org_tenants: false,
        capabilities: ["read", "write"], expires_at: credentialExpiry, revoked_at: null, bootstrap_allowed: false, fleet_id: null}}]));
    writeFileSync(cache, JSON.stringify({schema_version: 1, binding: {gateway: ORIGIN, api_prefix: "/api/v1",
      org_id: "org-a", tenant_id: "tenant-a", install_uuid: journal.install_uuid, hostname: hostname()}, entries}), {mode: 0o600});
  }
  return {run, calls, authority, cache, dir, readyCache};
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

for (const startFailure of ["network", "timeout", 503, 429])
  test(`managed startup recovers after a bounded cooldown: ${startFailure}`, () => {
    const f = fixture(); ok(f.run({action: "recover-start", plan: {startFailure}}));
    const probe = resources(f).find(r => r.url.endsWith("/whoami")); assert.ok(probe);
    assert.equal(probe.headers["x-api-key"], "fixture_install_key");
    assert.ok(f.calls().every(r => !r.url.includes("/agent-keys/provision")));
  });
for (const startFailure of ["network", "timeout", 503, 429])
  test(`managed boot retry starts heartbeat without restart: ${startFailure}`, () => {
    const f = fixture(); ok(f.run({action: "register-recovery", plan: {startFailure, recoverBoot: true},
      env: {CAURA_NODE_NAME: "fixture-node"}}));
  });

test("two warm agents on one node resolve concurrently", () => {
  const f = fixture(); f.readyCache(); ok(f.run({action: "warm-broker", request: {concurrent: true}}));
  assert.ok(f.calls().every(r => r.method !== "POST"));
});
for (const postInitFailure of ["network", 503, 429])
  for (const register of [false, true])
    test(`managed hooks fail fast with shared resolution; startup=${register}, failure=${postInitFailure}`, () => {
      const f = fixture(); ok(f.run({action: "tenant-fast-failure", request: {register}, plan: {postInitFailure},
        env: {CAURA_TENANT_ID: ""}}));
    });
for (const warm of [false, true])
  for (const cancel of [false, true])
    test(`same-agent signalled calls coalesce; warm=${warm}, cancel=${cancel}`, () => {
      const f = fixture(); if (warm) f.readyCache();
      ok(f.run({action: "signal-coalesce", request: {cancel}}));
      assert.equal(f.calls().filter(r => r.method === "POST" && r.url.includes("/admin/")).length, warm ? 0 : 1);
    });
for (const event of ["reuse", "elapsed", 401, 403, "authority", "held", "expired-key"])
  test(`five-minute ready-key window: ${event}`, () => {
    const f = fixture(), expires_at = new Date(Date.now() + 60_000).toISOString();
    f.readyCache(event === "expired-key" ? expires_at : undefined);
    ok(f.run({action: "reuse-window", request: {event},
      plan: event === "expired-key" ? {existing: {expires_at}} : {}}));
  });
for (const lock of ["fixture live mutation", "fixture killed mutation"])
  test(`warm ready key remains usable beside ${lock}`, () => {
    const f = fixture(); f.readyCache(); writeFileSync(f.cache + ".lock", lock, {mode: 0o600});
    ok(f.run({action: "warm-broker"})); assert.equal(readFileSync(f.cache + ".lock", "utf8"), lock);
    assert.ok(f.calls().every(r => r.method !== "POST"));
  });
test("a mutation waits for a legitimate writer to release the lock", () => {
  const f = fixture(); ok(f.run({action: "wait-broker"})); assert.ok(!existsSync(f.cache + ".lock"));
});
for (const pauseWarm of ["context", "metadata"])
  test(`recall abort reaches the warm ${pauseWarm} check`, () => {
    const f = fixture(); f.readyCache(); ok(f.run({action: "abort-warm", plan: {pauseWarm}}));
  });

for (const mode of ["managed", "standalone", "manual"])
  for (const op of ["read", "delete"])
    for (const doc_id of ["forge/abc", "notes.md", "a b", "x:y"])
      test(`document ${op} reaches server in ${mode}: ${doc_id}`, () => {
        const f = fixture();
        ok(f.run({mode, action: "tool", request: {tool: "caura_doc", params: {op, collection: "notes", doc_id}},
          env: {CAURA_MANUAL_AGENT_ID: "agent-b"}, plan: {manualReply: {agent_id: "agent-b"},
            response: {home_agent_id: "agent-b"}}}));
        const calls = resources(f).filter(r => new URL(r.url).pathname.includes("/documents/"));
        assert.equal(calls.length, 1); assert.equal(calls[0].method, op === "read" ? "GET" : "DELETE");
        assert.equal(new URL(calls[0].url).pathname, "/api/v1/documents/" + encodeURIComponent(doc_id));
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

for (const mode of [undefined, "", "unknown"])
  for (const reachability of ["unknown", "reachable", "unreachable"])
    test(`startup names the held auth mode once: mode=${mode}, backend=${reachability}`, () => {
      const f = fixture();
      const r = f.run({action: "startup", request: {reachability}, env: {CAURA_AUTH_MODE: mode}});
      ok(r);
      assert.deepEqual(f.calls(), [], "a held mode must make no request");
      assert.equal(r.diagnostics.length, 1,
        "registration and bootstrap share one visible notice: " + r.diagnostics.join("; "));
      const notice = r.diagnostics[0];
      assert.match(notice, /CAURA_AUTH_MODE/);
      assert.match(notice, /docs\/plugin-upgrade\.md/);
      assert.match(notice, mode === "unknown" ? /runtime_auth_mode_invalid/ : /runtime_auth_mode_required/);
      assert.doesNotMatch(notice, /Set CAURA_TENANT_ID|fixture prior outage/);
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
