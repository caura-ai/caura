/** Explicit enrollment is additive: no running agent adopts its authority yet. */
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync, closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync,
  rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { hostname, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { ENROLLMENT_PRELOAD } from "./install-authority.fixture.js";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

const DIST = dirname(fileURLToPath(import.meta.url));
const SRC = join(DIST, "../src");
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const BOOTSTRAP = "fixture_bootstrap_value";
const INSTALL = "fixture_install_value";
const REGISTRATION = "fixture_registration_value";
const dirs: string[] = [];
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });

function fixture() {
  const home = mkdtempSync(join(tmpdir(), "caura-enrollment-"));
  dirs.push(home);
  const dir = join(home, ".openclaw/plugins", FROZEN_PLUGIN_ID);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const journal = join(dir, ".installation-authority.json");
  const trace = join(home, "requests.jsonl");
  const preload = join(home, "preload.mjs");
  writeFileSync(preload, ENROLLMENT_PRELOAD);
  // Never let an import see a real operator's .env or credential store.
  const legacy = new Map([
    ["install.json", JSON.stringify({schema_version: 1, install_id: "abcdef123456", default_agent_id: "main"})],
    [".agent-keys.json", JSON.stringify({version: 1, keys: {main: {key: "fixture_legacy_agent"}}})],
    [".env", "CAURA_API_KEY=fixture_forbidden_tenant\nCAURA_FLEET_ID=local-fleet\n"],
  ]);
  for (const [name, value] of legacy) writeFileSync(join(dir, name), value, {mode: 0o600});
  const read = () => existsSync(journal) ? JSON.parse(readFileSync(journal, "utf8")) : null;
  const requests = () => existsSync(trace)
    ? readFileSync(trace, "utf8").trim().split("\n").filter(Boolean).map(s => JSON.parse(s)) : [];
  function run(input: unknown = {bootstrap_credential: BOOTSTRAP}, plan = {},
    args = ["register"], fd = 0, gateway = "https://enrollment.example.test") {
    const script = `
      import {existsSync} from 'node:fs';
      const entry = ${JSON.stringify(pathToFileURL(join(DIST, "enroll-installation.js")).href)};
      process.argv = [process.execPath, new URL(entry).pathname, ...process.argv.slice(1)];
      // On the old package, report the absent capability rather than throwing
      // ERR_MODULE_NOT_FOUND. Assertions below check protocol/state/diagnostics.
      if (existsSync(new URL(entry))) await import(entry);
      else { console.error('enrollment_unavailable'); process.exitCode = 2; }
    `;
    const inputPath = join(home, "synthetic-input.json");
    if (fd !== 0) writeFileSync(inputPath, JSON.stringify(input), {mode: 0o600});
    const inputFd = fd !== 0 ? openSync(inputPath, "r") : undefined;
    try { return spawnSync(process.execPath, ["--import", pathToFileURL(preload).href,
      "--input-type=module", "--eval", script, "--", ...args,
      "--gateway", gateway, "--expect-tenant", "tenant-a",
      ...(input === null ? [] : ["--credential-fd", String(fd)])], {
      env: {PATH: process.env.PATH, HOME: home, NODE_ENV: "test",
        CAURA_API_KEY: "fixture_forbidden_tenant", CAURA_FLEET_ID: "local-fleet",
        ENROLLMENT_TEST_JOURNAL: journal, ENROLLMENT_TEST_TRACE: trace,
        ENROLLMENT_TEST_PLAN: JSON.stringify(plan)},
      encoding: "utf8", timeout: 10_000, maxBuffer: 64 * 1024,
      ...(fd === 0 ? {input: input === null ? undefined : JSON.stringify(input)} :
        {stdio: ["ignore", "pipe", "pipe", inputFd!] as const}),
    }); } finally { if (inputFd !== undefined) closeSync(inputFd); }
  }
  const safeOutput = (r: ReturnType<typeof run>) => {
    for (const secret of [BOOTSTRAP, INSTALL, REGISTRATION, "fixture_forbidden_tenant", "fixture_server_body"])
      assert.ok(!`${r.stdout}${r.stderr}`.includes(secret), "diagnostics must not expose credentials or server bodies");
  };
  const unchanged = () => {
    for (const [name, value] of legacy) assert.equal(readFileSync(join(dir, name), "utf8"), value);
  };
  return {home, dir, journal, trace, run, read, requests, safeOutput, unchanged};
}

test("registration sends one credential, persists before context auth, and verifies server scope", () => {
  const f = fixture(); const r = f.run(); const [registration, context] = f.requests();
  assert.equal(f.requests().length, 2, "explicit registration must exchange then verify");
  assert.match(registration.body.install_uuid, UUID);
  assert.ok(registration.body.machine_fingerprint);
  assert.equal(registration.before.status, "pending");
  assert.equal(registration.before.install_uuid, registration.body.install_uuid);
  assert.equal(registration.headers["x-api-key"], BOOTSTRAP);
  assert.equal(registration.body.registration_key, undefined);
  assert.equal(context.headers["x-api-key"], INSTALL);
  assert.equal(context.before.install_credential, INSTALL);
  assert.equal(context.before.status, "credential_saved");
  for (const request of [registration, context]) {
    assert.equal(request.redirect, "error"); assert.equal(request.bounded, true);
    for (const header of ["x-install-uuid", "x-agent-id", "x-tenant-id", "x-gateway-secret"])
      assert.equal(request.headers[header], undefined);
  }
  assert.equal(r.status, 0);
  assert.equal(f.read().status, "verified");
  assert.equal(f.read().org_id, "org-a"); assert.equal(f.read().tenant_id, "tenant-a");
  assert.equal(f.read().fleet_id, "fleet-a");
  assert.equal(f.read().schema_version, 1);
  assert.equal(statSync(f.journal).mode & 0o777, 0o600);
  assert.ok(!readFileSync(f.journal, "utf8").includes(BOOTSTRAP));
  f.safeOutput(r); f.unchanged();
});

test("registration-key mode sends no bootstrap header and never saves the registration secret", () => {
  const f = fixture(); const r = f.run({registration_key: REGISTRATION});
  assert.equal(f.requests().length, 2);
  assert.equal(f.requests()[0].headers["x-api-key"], undefined);
  assert.equal(f.requests()[0].body.registration_key, REGISTRATION);
  assert.equal(r.status, 0);
  assert.ok(!readFileSync(f.journal, "utf8").includes(REGISTRATION)); f.safeOutput(r);
});

test("an explicitly passed descriptor works without a secret in argv or the environment", () => {
  const f = fixture(); const r = f.run({bootstrap_credential: BOOTSTRAP}, {}, ["register"], 3);
  assert.equal(f.requests().length, 2);
  assert.equal(f.requests()[0].headers["x-api-key"], BOOTSTRAP);
  assert.equal(r.status, 0); f.safeOutput(r);
});

for (const input of [{}, {bootstrap_credential: BOOTSTRAP, registration_key: REGISTRATION},
  {install_credential: INSTALL}, {bootstrap_credential: ""}]) {
  test(`register refuses ambiguous or missing authority (${Object.keys(input).join("+") || "empty"})`, () => {
    const f = fixture(); const r = f.run(input);
    assert.match(r.stderr, /enrollment_input_invalid/); assert.notEqual(r.status, 0);
    assert.deepEqual(f.requests(), []); assert.equal(f.read(), null); f.safeOutput(r);
  });
}

test("argv credentials are refused without echoing them or reading a tenant fallback", () => {
  const f = fixture(); const r = f.run({}, {}, ["register", "--bootstrap-credential", BOOTSTRAP]);
  assert.match(r.stderr, /enrollment_input_invalid/);
  assert.deepEqual(f.requests(), []); f.safeOutput(r);
});

test("lost registration acknowledgement replays the same persisted identity only on explicit invocation", () => {
  const f = fixture(); const first = f.run(undefined, {lostResponse: true});
  assert.equal(f.requests().length, 1);
  assert.notEqual(first.status, 0); assert.equal(f.read().status, "pending");
  const pending = f.read();
  assert.ok(!JSON.stringify(pending).includes(BOOTSTRAP));
  const second = f.run(); assert.equal(second.status, 0);
  assert.equal(f.requests()[1].body.install_uuid, pending.install_uuid);
  assert.equal(f.requests()[1].body.machine_fingerprint, pending.machine_fingerprint);
  f.safeOutput(first); f.safeOutput(second); f.unchanged();
});

test("a crash before context auth resumes from the saved credential without registering again", () => {
  const f = fixture(); const first = f.run(undefined, {crashAtContext: true});
  assert.equal(first.status, 71); assert.equal(f.read().status, "credential_saved");
  const n = f.requests().length;
  const blocked = f.run(null, {}, ["resume"]);
  assert.match(blocked.stderr, /enrollment_locked/); assert.equal(f.requests().length, n);
  // Synthetic operator recovery only: the production command must never
  // remove a stale lock itself. No real installation is touched by this test.
  rmSync(`${f.journal}.lock`);
  const second = f.run(null, {}, ["resume"]);
  assert.equal(second.status, 0);
  assert.equal(f.requests().length, n + 1);
  assert.ok(f.requests().at(-1).url.endsWith("/agent-connect-context"));
  assert.equal(f.read().status, "verified"); f.safeOutput(second);
});

test("failed explicit revalidation withholds previously verified authority", () => {
  const f = fixture(); assert.equal(f.run().status, 0);
  const r = f.run(null, {contextStatus: 403}, ["resume"]);
  assert.match(r.stderr, /enrollment_http_403/);
  assert.notEqual(f.read().status, "verified"); f.safeOutput(r);
});

test("legacy credential-free held journals stay readable and unusable without automatic recovery", () => {
  const f = fixture(); f.run(undefined, {lostResponse: true});
  const pending = f.read(); assert.equal(pending.status, "pending");
  writeFileSync(f.journal, JSON.stringify({...pending, status: "held"}));
  const n = f.requests().length;
  assert.equal(f.read().status, "held"); assert.equal(f.read().install_credential, undefined);
  assert.equal(f.read().install_uuid, pending.install_uuid);
  const moduleUrl = pathToFileURL(join(DIST, "install-authority.js")).href;
  const read = spawnSync(process.execPath, ["--input-type=module", "--eval", `
    const m = await import(${JSON.stringify(moduleUrl)});
    console.log(JSON.stringify(await m.readVerifiedInstallAuthority()));
  `], {env: {PATH: process.env.PATH, HOME: f.home}, encoding: "utf8", timeout: 5000});
  assert.equal(read.status, 0, "held state without a returned credential remains readable");
  assert.equal(read.stdout.trim(), "null");
  const bytes = readFileSync(f.journal, "utf8");
  const retry = f.run(); assert.match(retry.stderr, /enrollment_held/);
  assert.equal(readFileSync(f.journal, "utf8"), bytes);
  assert.equal(f.requests().length, n); f.safeOutput(retry); f.unchanged();
});

for (const status of ["pending", "credential_saved", "verified"]) {
  test(`local hostname drift preserves ${status} state and permits explicit retry after restoration`, () => {
    const f = fixture(); f.run(undefined, status === "pending" ? {lostResponse: true} :
      status === "credential_saved" ? {contextStatus: 503} : {});
    assert.equal(f.read().status, status);
    const before = readFileSync(f.journal, "utf8"); const n = f.requests().length;
    const input = status === "pending" ? {bootstrap_credential: BOOTSTRAP} : null;
    const mode = status === "pending" ? "register" : "resume";
    const r = f.run(input, {hostname: "temporary-hostname"}, [mode]);
    assert.match(r.stderr, /enrollment_scope_mismatch/); assert.notEqual(r.status, 0);
    assert.equal(readFileSync(f.journal, "utf8"), before); assert.equal(f.requests().length, n);
    const moduleUrl = pathToFileURL(join(DIST, "install-authority.js")).href;
    const read = spawnSync(process.execPath, ["--import", pathToFileURL(join(f.home, "preload.mjs")).href,
      "--input-type=module", "--eval", `const m = await import(${JSON.stringify(moduleUrl)});
        console.log(JSON.stringify(m.readVerifiedInstallAuthority()));`], {
      env: {PATH: process.env.PATH, HOME: f.home, ENROLLMENT_TEST_PLAN: JSON.stringify({hostname: "temporary-hostname"}),
        ENROLLMENT_TEST_JOURNAL: f.journal, ENROLLMENT_TEST_TRACE: f.trace}, encoding: "utf8", timeout: 5000});
    assert.equal(read.status, 0); assert.equal(read.stdout.trim(), "null");
    assert.equal(f.run(input, {}, [mode]).status, 0, "restoring the original host permits an explicit retry");
    assert.equal(f.read().install_uuid, JSON.parse(before).install_uuid);
    assert.equal(f.requests().length, n + (status === "pending" ? 2 : 1)); f.safeOutput(r); f.unchanged();
  });
}

test("transient descriptor reads and EOF preserve a complete credential without a fallback", () => {
  const f = fixture(); const r = f.run(undefined, {inputActions: ["EAGAIN", 5, "EWOULDBLOCK", "read", "EOF"]});
  assert.equal(r.status, 0); assert.equal(f.requests().length, 2);
  assert.equal(f.requests()[0].headers["x-api-key"], BOOTSTRAP); f.safeOutput(r);
});

test("permanently unavailable descriptor times out without enrollment or input disclosure", () => {
  const f = fixture(); const r = f.run(undefined, {inputWaitForever: true});
  assert.equal(r.status, 1); assert.match(r.stderr, /enrollment_input_invalid/);
  assert.equal(f.read(), null); assert.deepEqual(f.requests(), []); f.safeOutput(r);
});

for (const actions of [["EBADF"], [5, "EOF"]]) {
  test(`descriptor failure or truncated EOF refuses before enrollment (${actions.join("/")})`, () => {
    const f = fixture(); const r = f.run(undefined, {inputActions: actions});
    assert.equal(r.status, 1); assert.match(r.stderr, /enrollment_input_invalid/);
    assert.equal(f.read(), null); assert.deepEqual(f.requests(), []); f.safeOutput(r);
  });
}

test("unsupported Windows enrollment refuses before persistence or credential exchange", () => {
  const f = fixture(); const r = f.run(undefined, {platform: "win32"});
  assert.match(r.stderr, /enrollment_platform_unsupported/); assert.notEqual(r.status, 0);
  assert.equal(f.read(), null); assert.deepEqual(f.requests(), []);
  assert.ok(!existsSync(`${f.journal}.lock`)); f.safeOutput(r); f.unchanged();
});

test("optional credentials on held state do not relax usable-state or credential validation", () => {
  const f = fixture(); f.run(undefined, {lostResponse: true});
  const pending = f.read();
  const moduleUrl = pathToFileURL(join(DIST, "install-authority.js")).href;
  for (const extra of [{status: "credential_saved"},
    {status: "verified", org_id: "org-a", tenant_id: "tenant-a", fleet_id: "fleet-a"},
    {status: "held", install_credential: ""}]) {
    writeFileSync(f.journal, JSON.stringify({...pending, ...extra}));
    const read = spawnSync(process.execPath, ["--input-type=module", "--eval", `
      const m = await import(${JSON.stringify(moduleUrl)});
      try { m.readVerifiedInstallAuthority(); }
      catch(e) { console.error(e.message); process.exitCode = 1; }
    `], {env: {PATH: process.env.PATH, HOME: f.home}, encoding: "utf8", timeout: 5000});
    assert.equal(read.status, 1); assert.match(read.stderr, /enrollment_state_invalid/);
  }
});

for (const status of [401, 403, 404, 500]) {
  test(`context HTTP ${status} never retries registration or tenant authority`, () => {
    const f = fixture(); const r = f.run(undefined, {contextStatus: status});
    assert.match(r.stderr, new RegExp(`enrollment_http_${status}`));
    assert.equal(f.requests().length, 2); assert.notEqual(f.read()?.status, "verified");
    assert.notEqual(r.status, 0); f.safeOutput(r);
  });
}

test("failed credential persistence never authenticates with the returned secret", () => {
  const f = fixture(); const r = f.run(undefined, {failCredentialSave: true});
  assert.match(r.stderr, /enrollment_state_unwritable/);
  assert.equal(f.requests().length, 1); assert.equal(f.read().status, "pending");
  f.safeOutput(r);
});

for (const status of [401, 403, 404, 409, 429, 500]) {
  test(`registration HTTP ${status} never selects another endpoint or authority`, () => {
    const f = fixture(); const r = f.run(undefined, {registerStatus: status});
    assert.match(r.stderr, new RegExp(`enrollment_http_${status}`));
    assert.equal(f.requests().length, 1); assert.equal(f.read().status, "pending");
    assert.notEqual(r.status, 0); f.safeOutput(r);
  });
}

for (const [label, plan] of Object.entries({
  json: {invalidJson: true}, tenant: {registration: {tenant_id: "tenant-other"}},
  expiry: {registration: {expires_at: "2000-01-01T00:00:00Z"}},
  emptyCredential: {registration: {install_credential: ""}},
})) {
  test(`invalid registration ${label} cannot become verified or reach context`, () => {
    const f = fixture(); const r = f.run(undefined, plan);
    assert.match(r.stderr, /enrollment_(response_invalid|scope_mismatch|credential_expired)/);
    assert.equal(f.requests().length, 1);
    assert.notEqual(f.read()?.status, "verified"); f.safeOutput(r);
  });
}

for (const context of [{tenant_id: "tenant-other"}, {org_id: ""},
  {install_uuid: "22222222-2222-4222-8222-222222222222"}, {hostname: "different-host"}]) {
  test(`context ${Object.keys(context)[0]} must match authoritative and persisted scope`, () => {
    const f = fixture(); const r = f.run(undefined, {context});
    assert.equal(f.requests().length, 2); assert.notEqual(r.status, 0);
    assert.equal(f.read().status, "held");
    assert.match(r.stderr, /enrollment_(scope_mismatch|response_invalid)/); f.safeOutput(r);
  });
}

test("oversized context refuses with a bounded diagnostic", () => {
  const f = fixture(); const r = f.run(undefined, {hugeContext: true});
  assert.match(r.stderr, /enrollment_response_invalid/);
  assert.notEqual(f.read()?.status, "verified"); assert.ok(r.stderr.length < 1024); f.safeOutput(r);
});

test("handoff uses only context, adopts the verified UUID, and holds unknown fleet", () => {
  const f = fixture(); const r = f.run({install_credential: INSTALL}, {}, ["handoff"]);
  assert.equal(f.requests().length, 1);
  assert.ok(f.requests()[0].url.endsWith("/agent-connect-context"));
  assert.equal(f.requests()[0].headers["x-api-key"], INSTALL);
  assert.equal(f.requests()[0].before.install_credential, INSTALL);
  assert.equal(f.requests()[0].before.status, "credential_saved");
  assert.equal(f.read().install_uuid, "11111111-1111-4111-8111-111111111111");
  assert.equal(f.read().status, "held"); assert.equal(f.read().fleet_id, undefined);
  assert.match(r.stderr, /enrollment_fleet_unknown/); assert.notEqual(r.status, 0);
  f.safeOutput(r); f.unchanged();
});

test("registration cannot overwrite an explicitly expected fleet with another server fleet", () => {
  const f = fixture(); const r = f.run(undefined, {}, ["register", "--expect-fleet", "fleet-other"]);
  assert.match(r.stderr, /enrollment_scope_mismatch/);
  assert.equal(f.requests().length, 1); assert.notEqual(f.read()?.status, "verified");
});

test("a locally expected handoff fleet is not authority and cannot bypass the context hold", () => {
  const f = fixture();
  const r = f.run({install_credential: INSTALL}, {}, ["handoff", "--expect-fleet", "fleet-a"]);
  assert.equal(f.requests().length, 1); assert.equal(f.read().status, "held");
  assert.equal(f.read().fleet_id, undefined); assert.match(r.stderr, /enrollment_fleet_unknown/);
});

test("a saved gateway cannot be replaced to reuse the credential on another origin", () => {
  const f = fixture(); assert.equal(f.run().status, 0);
  const before = readFileSync(f.journal, "utf8"); const n = f.requests().length;
  const r = f.run(null, {}, ["resume"], 0, "https://different.example.test");
  assert.match(r.stderr, /enrollment_scope_mismatch/);
  assert.equal(f.requests().length, n); assert.equal(readFileSync(f.journal, "utf8"), before);
  f.safeOutput(r);
});

for (const [label, gateway] of Object.entries({
  cleartext: "http://enrollment.example.test", userinfo: "https://user:fixture_invalid@enrollment.example.test",
  query: "https://enrollment.example.test/?token=fixture_invalid", fragment: "https://enrollment.example.test/#other",
})) {
  test(`refuses ${label} gateway before any credential exchange`, () => {
    const f = fixture(); const r = f.run(undefined, {}, ["register"], 0, gateway);
    assert.match(r.stderr, /enrollment_gateway_invalid/);
    assert.deepEqual(f.requests(), []); assert.equal(f.read(), null); f.safeOutput(r);
  });
}

for (const kind of ["malformed", "schema", "permissions", "symlink", "lock"]) {
  test(`unsafe ${kind} state refuses without reset or network`, () => {
    const f = fixture(); const target = join(f.home, "unchanged.json");
    const pending = JSON.stringify({schema_version: 1, status: "pending",
      install_uuid: "11111111-1111-4111-8111-111111111111", machine_fingerprint: "a".repeat(64),
      gateway: "https://enrollment.example.test", api_prefix: "/api/v1", hostname: hostname(),
      platform: process.platform, arch: process.arch === "x64" ? "amd64" : "arm64",
      created_at: new Date().toISOString()});
    writeFileSync(target, pending, {mode: 0o600});
    if (kind === "symlink") symlinkSync(target, f.journal);
    else if (kind === "lock") writeFileSync(`${f.journal}.lock`, "operator recovery", {mode: 0o600});
    else {
      writeFileSync(f.journal, kind === "permissions" ? pending :
        kind === "malformed" ? "{" : JSON.stringify({schema_version: 99}), {mode: 0o600});
      if (kind === "permissions") chmodSync(f.journal, 0o644);
    }
    const before = kind === "lock" ? readFileSync(`${f.journal}.lock`, "utf8") : readFileSync(f.journal, "utf8");
    const r = f.run();
    assert.match(r.stderr, new RegExp(`enrollment_${kind === "lock" ? "locked" :
      ["permissions", "symlink"].includes(kind) ? "state_unsafe" : "state_invalid"}`));
    assert.deepEqual(f.requests(), []);
    assert.equal(readFileSync(kind === "lock" ? `${f.journal}.lock` : f.journal, "utf8"), before);
    assert.equal(readFileSync(target, "utf8"), pending); f.safeOutput(r); f.unchanged();
  });
}

test("only verified journal state is exposed through the authority reader", () => {
  const f = fixture(); const r = f.run(undefined, {crashAtContext: true});
  assert.equal(f.read()?.status, "credential_saved", "unverified state must exist before the reader check");
  const moduleUrl = pathToFileURL(join(DIST, "install-authority.js")).href;
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", `
    const m = await import(${JSON.stringify(moduleUrl)});
    console.log(JSON.stringify(await m.readVerifiedInstallAuthority()));
  `], {env: {PATH: process.env.PATH, HOME: f.home}, encoding: "utf8", timeout: 5000});
  assert.equal(result.status, 0); assert.equal(result.stdout.trim(), "null"); f.safeOutput(r);
});

test("the reader exposes verified scope but withholds held and expired state", () => {
  const f = fixture(); assert.equal(f.run().status, 0);
  const verified = f.read();
  const moduleUrl = pathToFileURL(join(DIST, "install-authority.js")).href;
  for (const [state, usable] of [[verified, true], [{...verified, status: "held"}, false],
    [{...verified, expires_at: "2000-01-01T00:00:00Z"}, false]] as const) {
    writeFileSync(f.journal, JSON.stringify(state), {mode: 0o600});
    const result = spawnSync(process.execPath, ["--input-type=module", "--eval", `
      const m = await import(${JSON.stringify(moduleUrl)});
      const p = await import(${JSON.stringify(pathToFileURL(join(DIST, "paths.js")).href)});
      if (p.getInstallAuthorityPath() !== ${JSON.stringify(f.journal)}) process.exit(9);
      console.log(JSON.stringify(await m.readVerifiedInstallAuthority()));
    `], {env: {PATH: process.env.PATH, HOME: f.home}, encoding: "utf8", timeout: 5000});
    assert.equal(result.status, 0);
    const value = JSON.parse(result.stdout);
    if (usable) assert.equal(value.install_uuid, verified.install_uuid);
    else assert.equal(value, null);
  }
});

test("runtime reads verified authority without importing the operator CLI", () => {
  const visited = new Set<string>();
  function inspect(name: string) {
    if (visited.has(name)) return; visited.add(name);
    assert.ok(name !== "enroll-installation.ts", `${name} must stay operator-only in this change`);
    const source = readFileSync(join(SRC, name), "utf8");
    if (name !== "paths.ts" && name !== "install-authority.ts") {
      assert.ok(!source.includes(".installation-authority.json"), `${name} reads the enrollment journal`);
      assert.ok(!source.includes("getInstallAuthorityPath"), `${name} imports its new private path`);
    }
    for (const m of source.matchAll(/(?:from\s+|import\s*\()\s*["']\.\/([^"']+)\.js["']/g)) {
      const child = `${m[1]}.ts`; if (existsSync(join(SRC, child))) inspect(child);
    }
  }
  for (const entry of ["transport.ts", "heartbeat.ts"]) inspect(entry);
  assert.ok(visited.size > 10, "inspect transitive runtime imports, not only the three entry files");
});

test("enrollment modules do not import env.ts or execute on a plain module import", () => {
  const f = fixture();
  const files = ["install-authority.ts", "enroll-installation.ts"];
  assert.ok(files.every(name => readdirSync(SRC).includes(name)), "the explicit enrollment implementation must ship");
  const visited = new Set<string>();
  function noEnv(name: string) {
    if (visited.has(name)) return; visited.add(name);
    assert.notEqual(name, "env.ts", "enrollment must not transitively load legacy environment secrets");
    const source = readFileSync(join(SRC, name), "utf8");
    for (const m of source.matchAll(/(?:from\s+|import\s*\()\s*["']\.\/([^"']+)\.js["']/g)) {
      const child = `${m[1]}.ts`; if (existsSync(join(SRC, child))) noEnv(child);
    }
  }
  files.forEach(noEnv);
  const r = spawnSync(process.execPath, ["--import", pathToFileURL(join(f.home, "preload.mjs")).href,
    "--input-type=module", "--eval", `await import(${JSON.stringify(pathToFileURL(join(DIST, "enroll-installation.js")).href)});`], {
    env: {PATH: process.env.PATH, HOME: f.home, ENROLLMENT_TEST_PLAN: "{}",
      ENROLLMENT_TEST_JOURNAL: f.journal, ENROLLMENT_TEST_TRACE: f.trace},
    encoding: "utf8", timeout: 5000,
  });
  assert.equal(r.status, 0); assert.deepEqual(f.requests(), []); assert.equal(f.read(), null); f.unchanged();
});
