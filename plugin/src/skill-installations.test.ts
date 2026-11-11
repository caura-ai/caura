/** Installation results survive outages and restarts without inventing success. */
import { test, beforeEach, afterEach, after, mock } from "node:test";
import assert from "node:assert/strict";
import fs, { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync, statSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

const originalHome = process.env.HOME;
const originalPath = process.env.PATH;
const home = mkdtempSync(join(tmpdir(), "caura-installations-test-"));
process.env.HOME = home;
process.env.CAURA_API_URL = "https://skill-installations.test";
process.env.CAURA_API_KEY = "installation-fixture-key";
process.env.CAURA_TENANT_ID = "installation-tenant";
process.env.CAURA_NODE_NAME = "installation-fixture-node";
delete process.env.CAURA_AGENT_ID;
delete process.env.CAURA_SKILL_TARGETS;
delete process.env.MEMCLAW_SKILL_TARGETS; // legacy-name-ok: isolate the supported legacy configuration alias
const bin = join(home, "bin");
mkdirSync(bin);
writeFileSync(join(bin, "openclaw"), "#!/bin/sh\nprintf 'OpenClaw fixture\\n'\n", { mode: 0o755 });
process.env.PATH = bin;
const { reconcileSkills } = await import("./reconcile-skills.js");
const { sendHeartbeat } = await import("./heartbeat.js");
const NODE = "042e88d1-e793-42f1-a61f-38221e574ef0";
const PEER = "359cf89b-03c0-4883-8cd1-9e4fb53a8215";
const GRANT = "b97fd117-7066-48bd-8bde-b841ef857728";
const pluginDir = join(home, ".openclaw", "plugins", FROZEN_PLUGIN_ID);
const skills = join(pluginDir, "skills");
// Private state retains event intents, pending results and approval bindings.
// Quarantine checks also inspect persisted origin and rejection metadata.
const outbox = join(pluginDir, "skill-installations.json");
const originalFetch = globalThis.fetch;
type Event = Record<string, unknown>;
type Call = { path: string; body: Record<string, unknown> };
let calls: Call[];
let reply: Record<string, unknown>;
let accept: "ok" | "offline" | "legacy" | "wrong-node" | "partial";
let verifyOnSubmit: (() => void) | undefined;
let resultStatus: number | undefined;

function hash(value: string): string {
  return "sha256:" + createHash("sha256").update(value, "utf8").digest("hex");
}
function entry(content = "# Approved installation\n", slug = "approved", grant = GRANT) {
  const artifact = `---\nname: ${JSON.stringify(slug)}\ndescription: "Reviewed fixture"\n---\n\n${content}`;
  return {
    doc_id: `agent/${slug}`,
    data: { name: slug, description: "Reviewed fixture", content },
    delivery: { grant_id: grant, artifact_hash: hash(artifact), bytes: Buffer.byteLength(artifact) },
  };
}
function manifest(documents = [entry()], extra: Record<string, unknown> = {}) {
  return { delivery_version: 1, node_id: NODE, complete: true, prune_allowed: true, held: [], documents, ...extra };
}
function results(): Event[] {
  return calls.filter((c) => c.path.endsWith("/skills/installations"))
    .flatMap((c) => c.body.events as Event[]);
}
function file(slug = "approved"): string { return join(skills, slug, "SKILL.md"); }
function plant(slug: string, text = "# Legacy copy\n"): void {
  mkdirSync(join(skills, slug), { recursive: true });
  writeFileSync(file(slug), text);
}
function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function ack(events: Event[], node = NODE): Record<string, unknown> {
  return { reporting_version: 1, node_id: node, accepted_event_ids: events.map((e) => e.event_id) };
}

beforeEach(() => {
  rmSync(pluginDir, { recursive: true, force: true });
  calls = [];
  reply = manifest();
  accept = "ok";
  verifyOnSubmit = undefined;
  resultStatus = undefined;
  delete process.env.CAURA_SKILL_TARGETS;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ path, body });
    if (path.endsWith("/skills/installable")) return response(reply);
    if (path.endsWith("/fleet/heartbeat")) return response({ node_id: NODE, commands: [] });
    if (path.endsWith("/skills/installations")) {
      verifyOnSubmit?.();
      if (resultStatus) return response({ detail: "fixture refusal" }, resultStatus);
      if (accept === "offline") return response({ detail: "fixture unavailable" }, 503);
      if (accept === "legacy") return response({ ok: true });
      if (accept === "wrong-node") return response(ack(body.events, PEER));
      if (accept === "partial") return response(ack([]));
      return response(ack(body.events, String(body.node_id)));
    }
    return response({ ok: true });
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  mock.restoreAll();
  syncBuiltinESMExports();
  delete process.env.CAURA_SKILL_TARGETS;
});
after(() => {
  process.env.PATH = originalPath;
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

test("a verified installation reports its exact approval after the file lands", async () => {
  const row = entry();
  verifyOnSubmit = () => assert.equal(hash(readFileSync(file(), "utf8")), row.delivery.artifact_hash);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  const ev = results()[0];
  assert.equal(ev.outcome, "installed");
  assert.equal(ev.skill_doc_id, row.doc_id);
  assert.equal(ev.grant_id, row.delivery.grant_id);
  assert.equal(ev.artifact_hash, row.delivery.artifact_hash);
  assert.match(String(ev.event_id), /^[0-9a-f-]{36}$/);
  assert.match(String(ev.target_id), /^[0-9a-f-]{36}$/);
  assert.ok(Number.isFinite(Date.parse(String(ev.occurred_at))));
  const request = calls.find((c) => c.path.endsWith("/skills/installations"))!;
  assert.equal(request.body.node_id, NODE);
  assert.equal(request.body.reporting_version, 1);
  const wire = JSON.stringify(request.body);
  assert.ok(!wire.includes(home), "absolute paths must stay local");
  assert.ok(!wire.includes("Approved installation"), "raw skill content must stay local");
  assert.ok(!("agent_id" in request.body), "attribution comes from the credential");
});

test("unchanged content does not produce another event", async () => {
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  calls = [];
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 0);
});

test("updates and removals retain their respective immutable artifact versions", async () => {
  await reconcileSkills({ nodeId: NODE });
  const first = results()[0];
  assert.ok(first, "initial materialization must be recorded");
  calls = [];
  const changed = entry("# New approved artifact\n", "approved", "929a9c66-ae75-4f38-924d-ae8d78980b5b");
  reply = manifest([changed]);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  assert.equal(results()[0].outcome, "updated");
  assert.equal(results()[0].grant_id, changed.delivery.grant_id);
  assert.equal(results()[0].target_id, first.target_id);
  calls = [];
  reply = manifest([]);
  verifyOnSubmit = () => assert.equal(existsSync(file()), false);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  assert.equal(results()[0].outcome, "removed");
  assert.equal(results()[0].grant_id, changed.delivery.grant_id);
  assert.equal(results()[0].artifact_hash, changed.delivery.artifact_hash);
  assert.equal(results()[0].target_id, first.target_id);
});

test("outages preserve a private bounded outbox and retry exactly the same IDs", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  const first = results()[0];
  assert.ok(existsSync(outbox), "the retry must survive losing process memory");
  assert.equal(statSync(outbox).mode & 0o777, 0o600);
  assert.ok(statSync(outbox).size <= 1024 * 1024);
  assert.ok(readFileSync(outbox, "utf8").includes(String(first.event_id)));
  calls = [];
  accept = "ok";
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  assert.deepEqual(results()[0], first);
  calls = [];
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 0, "a durable acknowledgment retires the pending result");
});

for (const malformed of ["legacy", "wrong-node", "partial"] as const) {
  test(`${malformed} acknowledgment cannot retire an event`, async () => {
    accept = malformed;
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    const first = results()[0];
    calls = [];
    accept = "ok";
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    assert.deepEqual(results()[0], first);
  });
}

// A new OS process proves persisted recovery, not a module-local retry queue.
// It receives only synthetic configuration and uses mocked HTTP.
function freshProcess(crash: "before" | "after" | false = false, nodeId = NODE): { status: number | null; events: Event[]; nodes: string[]; stderr: string } {
  const script = `
    import fs from "node:fs";
    import { syncBuiltinESMExports } from "node:module";
    const crash = ${JSON.stringify(crash)};
    if (crash) {
      const rename = fs.renameSync;
      fs.renameSync = (from, to) => {
        if (crash === "before" && String(to).endsWith("/SKILL.md")) process.exit(42);
        rename(from, to);
        if (String(to).endsWith("/SKILL.md")) process.exit(42);
      };
      syncBuiltinESMExports();
    }
    const events = [];
    const nodes = [];
    globalThis.fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      if (path.endsWith("/skills/installable")) return Response.json(${JSON.stringify(reply)});
      if (path.endsWith("/skills/installations")) {
        events.push(...body.events);
        nodes.push(body.node_id);
        return Response.json({reporting_version:1,node_id:body.node_id,accepted_event_ids:body.events.map(e=>e.event_id)});
      }
      return Response.json({ok:true});
    };
    const {reconcileSkills} = await import(${JSON.stringify(new URL("./reconcile-skills.js", import.meta.url).href)});
    await reconcileSkills({nodeId:${JSON.stringify(nodeId)}});
    console.log("INSTALLATION_CAPTURE=" + JSON.stringify(events));
    console.log("INSTALLATION_NODES=" + JSON.stringify(nodes));
  `;
  const child = spawnSync(process.execPath, ["--input-type=module", "--eval", script], {
    env: { HOME: home, NODE_ENV: "test", CAURA_API_URL: "https://skill-installations.test", CAURA_API_KEY: "installation-fixture-key", CAURA_TENANT_ID: "installation-tenant" },
    encoding: "utf8", timeout: 15_000,
  });
  const captured = child.stdout.split("\n").find((line) => line.startsWith("INSTALLATION_CAPTURE="));
  const nodes = child.stdout.split("\n").find((line) => line.startsWith("INSTALLATION_NODES="));
  return { status: child.status, events: captured ? JSON.parse(captured.slice("INSTALLATION_CAPTURE=".length)) : [],
    nodes: nodes ? JSON.parse(nodes.slice("INSTALLATION_NODES=".length)) : [], stderr: child.stderr };
}

test("pending results survive process restart with their original IDs", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  const child = freshProcess();
  assert.equal(child.status, 0, child.stderr);
  assert.deepEqual(child.events, [results()[0]]);
});

test("a changed heartbeat node cannot relabel pending results", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  const pendingId = results()[0].event_id;
  calls = [];
  reply = manifest([], { node_id: PEER, held: [{ doc_id: "agent/approved", reason: "approval_required" }] });
  await reconcileSkills({ nodeId: PEER });
  assert.equal(results().length, 1, "old-node results must actually be submitted while draining");
  for (const call of calls.filter((c) => c.path.endsWith("/skills/installations"))) {
    if ((call.body.events as Event[]).some((e) => e.event_id === pendingId)) {
      assert.equal(call.body.node_id, NODE, "the original receiver binding must survive retries");
    }
  }
  assert.ok(existsSync(outbox));
  assert.ok(readFileSync(outbox, "utf8").includes(String(pendingId)), "never silently drop the old receiver's result");
  assert.equal(JSON.parse(readFileSync(outbox, "utf8")).node_id, NODE);
  assert.equal(existsSync(file()), true, "mutations remain blocked while the old report is pending");
  calls = [];
  accept = "ok";
  reply = manifest([entry("# Replacement receiver\n", "replacement")], { node_id: PEER });
  await reconcileSkills({ nodeId: PEER });
  const oldReport = calls.find((call) => (call.body.events as Event[] | undefined)?.some((event) => event.event_id === pendingId));
  assert.equal(oldReport?.body.node_id, NODE);
  const newReport = calls.find((call) => call.path.endsWith("/skills/installations") && call.body.node_id === PEER);
  assert.equal((newReport?.body.events as Event[])[0].skill_doc_id, "agent/replacement");
  assert.ok((newReport?.body.events as Event[]).every((event) => event.event_id !== pendingId));
  assert.equal(existsSync(file()), false);
  assert.equal(existsSync(file("replacement")), true);
  assert.equal(JSON.parse(readFileSync(outbox, "utf8")).node_id, PEER);
});

test("drain-only journal methods refuse writes and removals until rebinding", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  const { SkillInstallations } = await import("./skill-installations.js");
  const journal = new SkillInstallations(PEER);
  const artifact = entry();
  let changed = false;
  assert.throws(() => journal.write(file(), {
    skill_doc_id: artifact.doc_id, grant_id: GRANT, artifact_hash: artifact.delivery.artifact_hash,
  }, () => { changed = true; }));
  assert.throws(() => journal.remove(file(), () => { changed = true; }));
  assert.equal(changed, false);
});

test("old-node draining and replacement progress survive a gateway restart", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  const original = results()[0];
  reply = manifest([entry("# Restarted replacement\n", "replacement")], { node_id: PEER });
  await reconcileSkills({ nodeId: PEER });
  const child = freshProcess(false, PEER);
  assert.equal(child.status, 0, child.stderr);
  assert.deepEqual(child.nodes, [NODE, PEER]);
  assert.deepEqual(child.events[0], original);
  assert.equal(child.events[1].skill_doc_id, "agent/replacement");
  const state = JSON.parse(readFileSync(outbox, "utf8"));
  assert.equal(state.node_id, PEER);
  assert.equal(state.events.length, 0);
});

for (const status of [401, 403, 404]) {
  test(`superseded receiver ${status} is retained locally without blocking its replacement`, async () => {
    accept = "offline";
    await reconcileSkills({ nodeId: NODE });
    const original = results()[0];
    calls = [];
    accept = "ok";
    const fetch = globalThis.fetch;
    globalThis.fetch = (async (input, init) => {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      if (String(input).endsWith("/skills/installations") && body.node_id === NODE) {
        calls.push({ path: new URL(String(input)).pathname, body });
        return response({ detail: "fixture old receiver unavailable" }, status);
      }
      return fetch(input, init);
    }) as typeof globalThis.fetch;
    reply = manifest([entry("# New receiver artifact\n", "replacement")], { node_id: PEER });
    await reconcileSkills({ nodeId: PEER });
    assert.ok(calls.some((call) => call.path.endsWith("/skills/installations") && call.body.node_id === NODE));
    assert.ok(calls.some((call) => call.path.endsWith("/skills/installations") && call.body.node_id === PEER));
    assert.equal(existsSync(file()), false, "the replacement can prune the prior receiver's orphan");
    assert.equal(existsSync(file("replacement")), true);
    const state = JSON.parse(readFileSync(outbox, "utf8"));
    assert.equal(state.node_id, PEER);
    assert.equal(state.events.length, 0);
    assert.equal(state.rejected.length, 1);
    assert.deepEqual(state.rejected[0].event, original);
    assert.equal(state.rejected[0].node_id, NODE);
    assert.equal(state.rejected[0].status, status);
    assert.equal(state.rejected[0].reason, "superseded_receiver");
    assert.equal(state.rejected[0].replacement_node_id, PEER);
  });
}

for (const crash of ["before", "after"] as const) {
  test(`a replacement recovers old-node intent ${crash} a completed rename`, async () => {
    const child = freshProcess(crash);
    assert.equal(child.status, 42, child.stderr);
    const reserved = JSON.parse(readFileSync(outbox, "utf8")).events[0].event;
    reply = manifest([], { node_id: PEER });
    await reconcileSkills({ nodeId: PEER });
    const reports = calls.filter((call) => call.path.endsWith("/skills/installations"));
    if (crash === "after") {
      assert.equal(reports.length, 1);
      assert.equal(reports[0].body.node_id, NODE);
      assert.equal((reports[0].body.events as Event[])[0].event_id, reserved.event_id);
    } else assert.equal(reports.length, 0, "an unperformed intent is not a successful installation");
    assert.equal(JSON.parse(readFileSync(outbox, "utf8")).node_id, PEER);
    assert.equal(existsSync(file()), false);
  });
}

test("a crash after atomic rename recovers the reserved event before a no-op", async () => {
  const child = freshProcess("after");
  assert.equal(child.status, 42, child.stderr);
  assert.equal(hash(readFileSync(file(), "utf8")), entry().delivery.artifact_hash);
  assert.ok(existsSync(outbox), "reserve durable event intent before changing the file");
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  assert.equal(results()[0].outcome, "installed");
  assert.equal(results()[0].artifact_hash, entry().delivery.artifact_hash);
});

test("an unfinished write intent is not reported as a completed installation", async () => {
  const child = freshProcess("before");
  assert.equal(child.status, 42, child.stderr);
  assert.equal(existsSync(file()), false);
  // Approval was withdrawn while the process was down. Recovery may retire
  // the unperformed intent, but it must not invent a completed write.
  reply = manifest([], { held: [{ doc_id: "agent/approved", reason: "approval_required" }] });
  await reconcileSkills({ nodeId: NODE });
  assert.equal(existsSync(file()), false);
  assert.equal(results().length, 0);
});

test("a full outbox pauses the next mutation without losing pending results", async () => {
  accept = "offline";
  for (let i = 0; i < 257; i++) {
    reply = manifest([entry(`# Revision ${i}\n`)]);
    await reconcileSkills({ nodeId: NODE });
  }
  assert.ok(readFileSync(file(), "utf8").includes("# Revision 255\n"));
  assert.ok(!readFileSync(file(), "utf8").includes("# Revision 256\n"));
  assert.ok(statSync(outbox).size <= 1024 * 1024);
  const ids = new Set(results().map((e) => e.event_id));
  assert.equal(ids.size, 256, "backpressure must preserve every reserved result");
  assert.ok(calls.filter((c) => c.path.endsWith("/skills/installations"))
    .every((c) => (c.body.events as Event[]).length <= 64));
  const shared = join(home, "full-outbox-additive");
  rmSync(shared, { recursive: true, force: true });
  process.env.CAURA_SKILL_TARGETS = JSON.stringify([{ dir: shared, mode: "additive" }]);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(existsSync(join(shared, "approved")), false, "backpressure must not leave an unowned empty skill directory");
});

test("concurrent gateway callers preserve one materialization and event", async () => {
  await Promise.all([reconcileSkills({ nodeId: NODE }), reconcileSkills({ nodeId: NODE })]);
  assert.equal(results().length, 1);
  assert.equal(results()[0].outcome, "installed");
});

for (const stage of ["reserve", "complete"] as const) {
  test(`journal ${stage} publication failure preserves intent without premature reporting`, async () => {
    const rename = fs.renameSync;
    let publications = 0;
    mock.method(fs, "renameSync", (from: fs.PathLike, to: fs.PathLike) => {
      if (String(to) === outbox && ++publications === (stage === "reserve" ? 1 : 2)) {
        throw new Error("fixture journal publication failure");
      }
      return rename(from, to);
    });
    syncBuiltinESMExports();
    await reconcileSkills({ nodeId: NODE });
    assert.equal(existsSync(file()), stage === "complete");
    assert.equal(results().length, 0);
    mock.restoreAll();
    syncBuiltinESMExports();
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    assert.equal(results()[0].outcome, "installed");
  });
}

for (const damage of ["corrupt", "oversized", "unwritable"] as const) {
  test(`${damage} outbox blocks mutation while heartbeat remains available`, async () => {
    mkdirSync(pluginDir, { recursive: true });
    if (damage === "unwritable") mkdirSync(outbox);
    else writeFileSync(outbox, damage === "corrupt" ? "{incomplete" : "x".repeat(1024 * 1024 + 1));
    await sendHeartbeat();
    assert.ok(calls.some((c) => c.path.endsWith("/fleet/heartbeat")));
    assert.equal(existsSync(file()), false, "cannot change disk without preserving result intent");
    assert.equal(results().length, 0);
  });
}

test("a failed atomic write never produces a successful installation result", async () => {
  const rename = fs.renameSync;
  mock.method(fs, "renameSync", (from: fs.PathLike, to: fs.PathLike) => {
    if (String(to).endsWith("/SKILL.md")) throw new Error("fixture rename failure");
    return rename(from, to);
  });
  syncBuiltinESMExports();
  await reconcileSkills({ nodeId: NODE });
  assert.equal(existsSync(file()), false);
  assert.equal(results().length, 0);
  mock.restoreAll();
  syncBuiltinESMExports();
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1, "report only the later successful write");
  assert.equal(results()[0].outcome, "installed");
});

test("a failed removal produces no removal event", async () => {
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  calls = [];
  reply = manifest([]);
  const remove = fs.rmSync;
  mock.method(fs, "rmSync", (path: fs.PathLike, options?: fs.RmOptions) => {
    if (String(path) === join(skills, "approved")) throw new Error("fixture removal failure");
    return remove(path, options);
  });
  syncBuiltinESMExports();
  await reconcileSkills({ nodeId: NODE });
  assert.equal(existsSync(file()), true);
  assert.equal(results().length, 0);
});

test("bad hashes, held legacy copies and unchanged preexisting files invent no results", async () => {
  plant("legacy");
  const bad = entry();
  bad.delivery.artifact_hash = "sha256:" + "0".repeat(64);
  reply = manifest([bad]);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(existsSync(file()), false);
  assert.equal(results().length, 0);
  reply = manifest([], { held: [{ doc_id: "agent/legacy", reason: "approval_required" }] });
  await reconcileSkills({ nodeId: NODE });
  assert.equal(readFileSync(file("legacy"), "utf8"), "# Legacy copy\n");
  assert.equal(results().length, 0);
  const preexisting = entry();
  plant("approved", `---\nname: "approved"\ndescription: "Reviewed fixture"\n---\n\n${preexisting.data.content}`);
  reply = manifest();
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 0, "an unchanged legacy file has no new materialization event");
});

test("additive collisions report only real writes without private directories", async () => {
  const shared = join(home, "shared-skills");
  rmSync(shared, { recursive: true, force: true });
  mkdirSync(join(shared, "approved"), { recursive: true });
  writeFileSync(join(shared, "approved", "SKILL.md"), "# Foreign copy\n");
  process.env.CAURA_SKILL_TARGETS = JSON.stringify([{ dir: shared, mode: "additive" }]);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1, "only the owned target was changed");
  assert.equal(readFileSync(join(shared, "approved", "SKILL.md"), "utf8"), "# Foreign copy\n");
  assert.ok(!JSON.stringify(results()).includes(shared));
  calls = [];
  rmSync(join(shared, "approved"), { recursive: true });
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1, "only the new additive copy changed");
  assert.equal(results()[0].outcome, "installed");
});

test("two real targets get distinct opaque IDs without leaking private directories", async () => {
  const shared = join(home, "second-private-skills");
  rmSync(shared, { recursive: true, force: true });
  process.env.CAURA_SKILL_TARGETS = JSON.stringify([{ dir: shared, mode: "additive" }]);
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 2);
  assert.notEqual(results()[0].target_id, results()[1].target_id);
  assert.ok(!JSON.stringify(results()).includes(home));
});

test("an idle journal can bind a new node without relabeling old installations", async () => {
  await reconcileSkills({ nodeId: NODE });
  assert.equal(results().length, 1);
  calls = [];
  // The unchanged old file is not a new installation for the new receiver.
  reply = manifest([entry()], { node_id: PEER });
  await reconcileSkills({ nodeId: PEER });
  assert.equal(results().length, 0);
  assert.equal(JSON.parse(readFileSync(outbox, "utf8")).node_id, PEER);
  // Its old node's approval must not later be attributed to this receiver.
  reply = manifest([], { node_id: PEER });
  await reconcileSkills({ nodeId: PEER });
  assert.equal(existsSync(file()), false);
  assert.equal(results().length, 0);
});

for (const damage of ["version", "tenant", "structure"] as const) {
  test(`an idle node change does not reset ${damage} state`, async () => {
    await reconcileSkills({ nodeId: NODE });
    const state = JSON.parse(readFileSync(outbox, "utf8"));
    if (damage === "version") state.version = 99;
    else if (damage === "tenant") state.tenant_id = "different-tenant";
    else state.installed = [null];
    const original = JSON.stringify(state);
    writeFileSync(outbox, original, { mode: 0o600 });
    calls = [];
    reply = manifest([], { node_id: PEER });
    await reconcileSkills({ nodeId: PEER });
    assert.equal(readFileSync(outbox, "utf8"), original);
    assert.equal(existsSync(file()), true);
    assert.equal(results().length, 0);
  });
}

for (const [kind, grant] of [["non-UUID", "not-a-uuid"], ["trailing-newline", GRANT + "\n"]]) {
  test(`a ${kind} new grant cannot mutate files or create a pending report`, async () => {
    plant("orphan");
    reply = manifest([entry("# Invalid grant\n", "approved", grant)]);
    await reconcileSkills({ nodeId: NODE });
    assert.equal(existsSync(file()), false);
    assert.equal(existsSync(file("orphan")), true);
    assert.equal(results().length, 0);
  });
}

for (const status of [409, 422]) {
  test(`${status} isolates rejected reports without blocking valid batch neighbors`, async () => {
    accept = "offline";
    await reconcileSkills({ nodeId: NODE });
    const rejected = results()[0];
    reply = manifest([entry("# Later version\n")]);
    await reconcileSkills({ nodeId: NODE });
    const valid = [...new Map(results().map((event) => [event.event_id, event])).values()]
      .find((event) => event.event_id !== rejected.event_id)!;
    calls = [];
    accept = "ok";
    const fetch = globalThis.fetch;
    globalThis.fetch = (async (input, init) => {
      if (String(input).endsWith("/skills/installations")) {
        const events = JSON.parse(String(init?.body)).events as Event[];
        if (events.some((event) => event.event_id === rejected.event_id)) {
          const path = new URL(String(input)).pathname;
          calls.push({ path, body: JSON.parse(String(init?.body)) });
          return response({ detail: "fixture permanent refusal" }, status);
        }
      }
      return fetch(input, init);
    }) as typeof globalThis.fetch;
    await reconcileSkills({ nodeId: NODE });
    assert.ok(calls.some((call) => (call.body.events as Event[] | undefined)?.length === 1
      && (call.body.events as Event[])[0].event_id === valid.event_id), "isolate and accept the valid report");
    const persisted = readFileSync(outbox, "utf8");
    assert.ok(persisted.includes(String(rejected.event_id)), "preserve rejected evidence locally");
    calls = [];
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 0, "accepted and quarantined reports leave the active outbox");
    const restarted = freshProcess();
    assert.equal(restarted.status, 0, restarted.stderr);
    assert.equal(restarted.events.length, 0, "quarantine survives restart without a fake acceptance");
    // The old rejection retains its original origin after an idle rebind.
    reply = manifest([entry("# Later version\n")], { node_id: PEER });
    await reconcileSkills({ nodeId: PEER });
    const state = JSON.parse(readFileSync(outbox, "utf8"));
    assert.equal(state.node_id, PEER);
    assert.equal(state.rejected[0].node_id, NODE);
    assert.equal(state.rejected[0].event.event_id, rejected.event_id);
  });
}

for (const status of [401, 403, 404, 408, 429, 500, 503]) {
  test(`${status} remains pending and can recover later`, async () => {
    resultStatus = status;
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    const first = results()[0];
    calls = [];
    resultStatus = undefined;
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    assert.deepEqual(results()[0], first);
  });
}

for (const failure of ["network", "timeout"] as const) {
  test(`${failure} errors preserve the original report for retry`, async () => {
    const fetch = globalThis.fetch;
    globalThis.fetch = (async (input, init) => {
      if (String(input).endsWith("/skills/installations")) {
        calls.push({ path: new URL(String(input)).pathname, body: JSON.parse(String(init?.body)) });
        if (failure === "timeout") throw new DOMException("fixture timeout", "TimeoutError");
        throw new TypeError("fixture network failure");
      }
      return fetch(input, init);
    }) as typeof globalThis.fetch;
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    const original = results()[0];
    globalThis.fetch = fetch;
    calls = [];
    await reconcileSkills({ nodeId: NODE });
    assert.equal(results().length, 1);
    assert.deepEqual(results()[0], original);
  });
}

test("permanent-rejection isolation has a per-tick request bound", async () => {
  accept = "offline";
  for (let i = 0; i < 12; i++) {
    reply = manifest([entry(`# Bounded rejection ${i}\n`)]);
    await reconcileSkills({ nodeId: NODE });
  }
  calls = [];
  resultStatus = 409;
  await reconcileSkills({ nodeId: NODE });
  const attempts = calls.filter((call) => call.path.endsWith("/skills/installations"));
  assert.ok(attempts.length > 1, "isolation must make progress");
  assert.ok(attempts.length <= 8, "isolation must not hold heartbeat behind 64 timeouts");
});

test("a full rejected archive preserves further refusals in the active outbox", async () => {
  resultStatus = 409;
  for (let i = 0; i < 257; i++) {
    reply = manifest([entry(`# Rejected revision ${i}\n`)]);
    await reconcileSkills({ nodeId: NODE });
  }
  const state = JSON.parse(readFileSync(outbox, "utf8"));
  assert.equal(state.rejected.length, 256);
  assert.equal(state.events.length, 1, "do not silently evict rejected history when bounded storage fills");
  assert.ok(statSync(outbox).size <= 1024 * 1024);
  const pending = state.events[0].event;
  calls = [];
  resultStatus = undefined;
  await reconcileSkills({ nodeId: NODE });
  assert.ok(results().some((event) => event.event_id === pending.event_id));
  resultStatus = 409;
  reply = manifest([entry("# Retained old receiver report\n")]);
  await reconcileSkills({ nodeId: NODE });
  calls = [];
  resultStatus = 401;
  reply = manifest([], { node_id: PEER });
  await reconcileSkills({ nodeId: PEER });
  assert.equal(results().length, 1, "even a full archive must attempt the original receiver");
  const held = JSON.parse(readFileSync(outbox, "utf8"));
  assert.equal(held.node_id, NODE);
  assert.equal(held.rejected.length, 256);
  assert.equal(held.events.length, 1, "do not discard evidence to force a rebind");
  assert.ok(readFileSync(file(), "utf8").includes("Retained old receiver report"));
});

test("legacy malformed pending grants are isolated rather than locking the journal", async () => {
  accept = "offline";
  await reconcileSkills({ nodeId: NODE });
  const state = JSON.parse(readFileSync(outbox, "utf8"));
  const originalId = state.events[0].event.event_id;
  state.events[0].event.grant_id = "legacy-non-uuid";
  writeFileSync(outbox, JSON.stringify(state), { mode: 0o600 });
  calls = [];
  resultStatus = 422;
  await reconcileSkills({ nodeId: NODE });
  const rejected = JSON.parse(readFileSync(outbox, "utf8"));
  assert.equal(rejected.rejected.length, 1);
  assert.equal(rejected.rejected[0].event.event_id, originalId);
  assert.equal(rejected.events.length, 0);
});
