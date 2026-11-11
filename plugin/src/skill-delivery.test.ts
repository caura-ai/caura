/** Receiver-bound delivery, byte ceilings, and non-destructive legacy holds. */
import { test, beforeEach, afterEach, after } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FROZEN_PLUGIN_ID } from "./legacy-contracts.fixture.js";

process.env.CAURA_API_KEY = "delivery-test-fixture";
process.env.CAURA_API_URL = "https://skill-delivery.test";
process.env.CAURA_TENANT_ID = "delivery-tenant";
process.env.CAURA_FLEET_ID = "asserted-fleet";
delete process.env.CAURA_AGENT_ID;
const originalHome = process.env.HOME;
const home = mkdtempSync(join(tmpdir(), "caura-delivery-test-"));
process.env.HOME = home;
const { reconcileSkills } = await import("./reconcile-skills.js");
const { apiCall } = await import("./transport.js");
// The existing zero-argument function is assignable to this optional-input
// contract, so main compiles and fails behaviorally rather than at collection.
const reconcile: (receiver?: { nodeId: string }) => ReturnType<typeof reconcileSkills> = reconcileSkills;
const NODE = "042e88d1-e793-42f1-a61f-38221e574ef0";
const PEER = "359cf89b-03c0-4883-8cd1-9e4fb53a8215";
const FILE_MAX = 65_536;
const RESPONSE_MAX = 4 * 1024 * 1024;
const skills = join(home, ".openclaw", "plugins", FROZEN_PLUGIN_ID, "skills");
const originalFetch = globalThis.fetch;
let reply: unknown;
let respond: (() => Response) | undefined;
let calls: Array<{ path: string; body: Record<string, unknown> }>;

function hash(text: string): string {
  return "sha256:" + createHash("sha256").update(text, "utf8").digest("hex");
}
function materialized(data: { name: string; description: string; content: string }): string {
  if (data.content.startsWith("---\n")) return data.content;
  return `---\nname: ${JSON.stringify(data.name)}\ndescription: ${JSON.stringify(data.description)}\n---\n\n${data.content}`;
}
function entry(content = "# Reviewed fixture\n", slug = "approved") {
  const data = { name: slug, description: "Use the approved delivery fixture.", content };
  const artifact = materialized(data);
  return {
    doc_id: `agent/${slug}`,
    data,
    delivery: { grant_id: "grant-fixture", artifact_hash: hash(artifact), bytes: Buffer.byteLength(artifact, "utf8") },
  };
}
function manifest(documents: unknown[] = [entry()], extra: Record<string, unknown> = {}) {
  return { delivery_version: 1, node_id: NODE, complete: true, prune_allowed: true, documents, held: [], ...extra };
}
function plant(slug: string, content = "# Existing copy\n"): void {
  mkdirSync(join(skills, slug), { recursive: true });
  writeFileSync(join(skills, slug, "SKILL.md"), content);
}
function present(slug: string): boolean { return existsSync(join(skills, slug, "SKILL.md")); }
function read(slug: string): string { return readFileSync(join(skills, slug, "SKILL.md"), "utf8"); }

beforeEach(() => {
  rmSync(skills, { recursive: true, force: true });
  calls = [];
  reply = manifest();
  respond = undefined;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) : {} });
    if (!path.endsWith("/skills/installable")) return new Response("unexpected endpoint", { status: 500 });
    return respond ? respond() : new Response(JSON.stringify(reply), { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = originalFetch; });
after(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

test("an approved artifact is installed byte-for-byte", async () => {
  const row = entry();
  const summary = await reconcile({ nodeId: NODE });
  assert.equal(read("approved"), materialized(row.data));
  assert.deepEqual(summary.installed, ["approved"]);
});

test("the pull names its storage-issued node and delivery contract", async () => {
  await reconcile({ nodeId: NODE });
  const request = calls.find((c) => c.path.endsWith("/skills/installable"));
  assert.equal(request?.body.node_id, NODE);
  assert.equal(request?.body.delivery_version, 1);
});

test("no receiver identity means no catalog pull or materialization", async () => {
  plant("legacy");
  await reconcile();
  assert.equal(calls.filter((c) => c.path.endsWith("/skills/installable")).length, 0);
  assert.equal(read("legacy"), "# Existing copy\n");
  assert.equal(present("approved"), false);
});

for (const kind of ["legacy-array", "unversioned", "incomplete", "wrong-version", "wrong-node"] as const) {
  test(`${kind} cannot mutate the installed set`, async () => {
    plant("legacy");
    if (kind === "legacy-array") reply = [entry()];
    else if (kind === "unversioned") reply = { documents: [entry()] };
    else if (kind === "incomplete") reply = manifest([entry()], { complete: false });
    else if (kind === "wrong-version") reply = manifest([entry()], { delivery_version: 99 });
    else reply = manifest([entry()], { node_id: PEER });
    await reconcile({ nodeId: NODE });
    assert.equal(read("legacy"), "# Existing copy\n");
    assert.equal(present("approved"), false);
    assert.ok(calls.every((c) => !c.path.includes("/documents")), "delivery must not fall back to a generic catalog");
  });
}

for (const kind of ["missing-grant", "wrong-hash", "wrong-byte-count", "invalid-id"] as const) {
  test(`${kind} cannot write or prune`, async () => {
    plant("legacy");
    const row: Record<string, unknown> = entry();
    if (kind === "missing-grant") delete row.delivery;
    else if (kind === "invalid-id") row.doc_id = "agent/approved\n";
    else row.delivery = { ...entry().delivery, ...(kind === "wrong-hash" ? { artifact_hash: "sha256:" + "0".repeat(64) } : { bytes: 1 }) };
    reply = manifest([row]);
    await reconcile({ nodeId: NODE });
    assert.equal(read("legacy"), "# Existing copy\n");
    assert.equal(present("approved"), false);
  });
}

test("a later invalid artifact prevents earlier writes in the same manifest", async () => {
  plant("legacy");
  const invalid = entry("# Invalid second artifact\n", "invalid");
  invalid.delivery.artifact_hash = "sha256:" + "0".repeat(64);
  reply = manifest([entry(), invalid]);
  await reconcile({ nodeId: NODE });
  assert.equal(present("approved"), false);
  assert.equal(present("invalid"), false);
  assert.equal(read("legacy"), "# Existing copy\n");
});

test("held legacy copies remain without becoming approved installations", async () => {
  plant("legacy");
  reply = manifest([], { held: [{ doc_id: "agent/legacy", reason: "approval_required" }, { doc_id: "agent/not-present", reason: "approval_required" }] });
  const summary = await reconcile({ nodeId: NODE });
  assert.equal(read("legacy"), "# Existing copy\n");
  assert.equal(present("not-present"), false);
  assert.deepEqual(summary.installed, []);
});

test("a complete approved empty manifest can prune an unheld orphan", async () => {
  plant("orphan");
  plant(FROZEN_PLUGIN_ID, "# Bundled\n");
  reply = manifest([]);
  await reconcile({ nodeId: NODE });
  assert.equal(present("orphan"), false);
  assert.equal(read(FROZEN_PLUGIN_ID), "# Bundled\n");
});

function sizedArtifact(bytes: number, glyph: string): string {
  const prefix = "---\nname: boundary\ndescription: Bound byte fixture\n---\n\n";
  const remaining = bytes - Buffer.byteLength(prefix);
  const width = Buffer.byteLength(glyph);
  return prefix + glyph.repeat(Math.floor(remaining / width)) + "x".repeat(remaining % width);
}
for (const glyph of ["x", "界"]) {
  for (const bytes of [FILE_MAX, FILE_MAX + 1]) {
    test(`rendered ${bytes} UTF-8 bytes with ${glyph === "x" ? "ASCII" : "multibyte text"}`, async () => {
      const artifact = sizedArtifact(bytes, glyph);
      assert.equal(Buffer.byteLength(artifact), bytes);
      reply = manifest([entry(artifact)]);
      await reconcile({ nodeId: NODE });
      assert.equal(present("approved"), bytes <= FILE_MAX);
      if (bytes <= FILE_MAX) assert.equal(read("approved"), artifact);
    });
  }
}

test("generated frontmatter counts toward the final file byte ceiling", async () => {
  const row = entry("x".repeat(FILE_MAX));
  assert.ok(Buffer.byteLength(materialized(row.data)) > FILE_MAX);
  reply = manifest([row]);
  await reconcile({ nodeId: NODE });
  assert.equal(present("approved"), false);
});

function jsonBytes(bytes: number): string {
  const empty = JSON.stringify({ ok: true, padding: "" });
  return JSON.stringify({ ok: true, padding: "x".repeat(bytes - Buffer.byteLength(empty)) });
}

test("the transport accepts a complete response at the exact 4 MiB boundary", async () => {
  const text = jsonBytes(RESPONSE_MAX);
  respond = () => new Response(text, { headers: { "Content-Type": "application/json", "Content-Length": String(RESPONSE_MAX) } });
  const result = await apiCall("POST", "/skills/installable", { node_id: NODE, delivery_version: 1 }) as { ok: boolean };
  assert.equal(result.ok, true);
});

test("one byte beyond the response ceiling is refused", async () => {
  respond = () => new Response(jsonBytes(RESPONSE_MAX + 1), { headers: { "Content-Type": "application/json" } });
  await assert.rejects(apiCall("POST", "/skills/installable", { node_id: NODE, delivery_version: 1 }));
});

for (const advertised of [undefined, "1", String(RESPONSE_MAX + 1)]) {
  test(`oversized stream is cancelled with Content-Length ${advertised ?? "absent"}`, async () => {
    const text = new TextEncoder().encode(jsonBytes(6 * 1024 * 1024));
    let offset = 0;
    let cancelled = false;
    respond = () => new Response(new ReadableStream<Uint8Array>({
      pull(controller) {
        if (offset >= text.byteLength) { controller.close(); return; }
        controller.enqueue(text.slice(offset, offset + 65536));
        offset += 65536;
      },
      cancel() { cancelled = true; },
    }), { headers: { "Content-Type": "application/json", ...(advertised === undefined ? {} : { "Content-Length": advertised }) } });
    await assert.rejects(apiCall("POST", "/skills/installable", { node_id: NODE, delivery_version: 1 }));
    assert.equal(cancelled, true);
    assert.ok(offset < text.byteLength, "the limit must stop reading before the whole body is buffered");
  });
}

test("oversized error bodies are bounded too", async () => {
  const text = new TextEncoder().encode(jsonBytes(6 * 1024 * 1024));
  let offset = 0;
  let cancelled = false;
  respond = () => new Response(new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= text.byteLength) { controller.close(); return; }
      controller.enqueue(text.slice(offset, offset + 65536));
      offset += 65536;
    },
    cancel() { cancelled = true; },
  }), { status: 500 });
  await assert.rejects(apiCall("POST", "/skills/installable", { node_id: NODE, delivery_version: 1 }));
  assert.equal(cancelled, true);
  assert.ok(offset < text.byteLength);
});

test("a truncated response cannot remove existing files", async () => {
  plant("legacy");
  respond = () => new Response('{"delivery_version":1,"documents":[', { headers: { "Content-Type": "application/json" } });
  await reconcile({ nodeId: NODE });
  assert.equal(read("legacy"), "# Existing copy\n");
});


type MetadataVector = {
  id: string;
  canonical: { name: string; description: string; content: string };
  artifact: string;
  artifact_hash: string;
  bytes: number;
};
const metadataVectors = JSON.parse(readFileSync(
  new URL("../../tests/fixtures/skill-delivery-metadata.json", import.meta.url), "utf8",
)) as MetadataVector[];
for (const vector of metadataVectors) {
  test(`shared metadata vector: ${vector.id}`, async () => {
    reply = manifest([{
      doc_id: "agent/approved", data: vector.canonical,
      delivery: { grant_id: "shared-vector", artifact_hash: vector.artifact_hash, bytes: vector.bytes },
    }]);
    const summary = await reconcile({ nodeId: NODE });
    assert.deepEqual(summary.installed, ["approved"]);
    assert.equal(read("approved"), vector.artifact);
    assert.equal(hash(read("approved")), vector.artifact_hash);
    assert.equal(Buffer.byteLength(read("approved")), vector.bytes);
  });
}


test("an unsafe catalog identity can suppress pruning while approved updates continue", async () => {
  plant("legacy");
  plant("approved", "# Previous approved version\n");
  reply = manifest([entry()], { prune_allowed: false });
  const summary = await reconcile({ nodeId: NODE });
  assert.equal(read("approved"), materialized(entry().data));
  assert.equal(read("legacy"), "# Existing copy\n");
  assert.deepEqual(summary.removed, []);
  assert.deepEqual(summary.installed, ["approved"]);
});

for (const hint of [undefined, "false", 0]) {
  test(`invalid pruning permission ${String(hint)} preserves all disk state`, async () => {
    plant("legacy");
    reply = manifest([entry()], { prune_allowed: hint });
    await reconcile({ nodeId: NODE });
    assert.equal(read("legacy"), "# Existing copy\n");
    assert.equal(present("approved"), false);
  });
}

test("pruning suppression also protects marker-owned additive copies", async () => {
  const { OWNED_MARKER } = await import("./reconcile-skills.js");
  const target = join(home, "additional", "skills");
  mkdirSync(join(target, "legacy"), { recursive: true });
  writeFileSync(join(target, "legacy", "SKILL.md"), "# Existing additive copy\n");
  writeFileSync(join(target, "legacy", OWNED_MARKER), "owned");
  const previous = process.env.CAURA_SKILL_TARGETS;
  process.env.CAURA_SKILL_TARGETS = JSON.stringify([{ dir: target, mode: "additive" }]);
  try {
    reply = manifest([entry()], { prune_allowed: false });
    const summary = await reconcile({ nodeId: NODE });
    assert.equal(readFileSync(join(target, "legacy", "SKILL.md"), "utf8"), "# Existing additive copy\n");
    assert.equal(readFileSync(join(target, "approved", "SKILL.md"), "utf8"), materialized(entry().data));
    assert.deepEqual(summary.removed, []);
  } finally {
    if (previous === undefined) delete process.env.CAURA_SKILL_TARGETS;
    else process.env.CAURA_SKILL_TARGETS = previous;
  }
});
