/**
 * Tests for the ``interview_request`` command handler (Interviewer Phase 1).
 *
 * Drives ``processCommand`` via ``__DEPLOY_INTERNALS__`` with a mocked
 * ``globalThis.fetch``, pinning the contract frozen in PR #558:
 * - disabled node / missing node_id → status=failed, buffer untouched;
 * - empty buffer → status=done, submitted:false, no submit call;
 * - happy path → one POST /interview/submit with the exact window
 *   (cursor_from/to = first/last seq, command_id echoed), then prune
 *   through the committed watermark;
 * - ANY submit failure (non-2xx → apiCall throws) → status=failed and
 *   the buffer is NOT pruned (the no-loss invariant).
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

// Pattern matches ``keystones.test.ts`` / ``transport.test.ts``: env is
// fixed BEFORE the dynamic import so env.js captures it (its exports are
// bound at module-evaluation time — a static import would evaluate first).
process.env.MEMCLAW_API_KEY = "mc_test_interview";
process.env.MEMCLAW_API_URL = "http://localhost:8000";
process.env.MEMCLAW_TENANT_ID = "t-test";

const { __DEPLOY_INTERNALS__ } = await import("./heartbeat.js");
const { appendInterviewEvent, readInterviewEvents, __INTERVIEW_BUFFER_INTERNALS__ } =
  await import("./interview-buffer.js");

interface CapturedRequest {
  url: string;
  method: string;
  body: Record<string, unknown> | undefined;
}

describe("interview_request command handler", () => {
  let tmp: string;
  let captured: CapturedRequest[];
  let submitResponse: { status: number; body: Record<string, unknown> };
  let originalFetch: typeof fetch;

  function findRequest(pathPart: string): CapturedRequest | undefined {
    return captured.find((r) => r.url.includes(pathPart));
  }

  function resultPost(): CapturedRequest | undefined {
    return captured.find((r) => r.url.includes("/fleet/commands/") && r.url.endsWith("/result"));
  }

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "memclaw-interview-cmd-"));
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(join(tmp, "buf.jsonl"));
    __DEPLOY_INTERNALS__.setInterviewerEnabledForTests(true);
    captured = [];
    submitResponse = {
      status: 200,
      body: { status: "committed", watermark: 999, memories_written: 3 },
    };
    originalFetch = globalThis.fetch;
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
      captured.push({ url, method: init?.method || "GET", body });
      if (url.includes("/auth/verify")) {
        return new Response(JSON.stringify({ tenant_id: "t-test" }), { status: 200 });
      }
      if (url.includes("/interview/submit")) {
        return new Response(JSON.stringify(submitResponse.body), {
          status: submitResponse.status,
        });
      }
      // /fleet/commands/{id}/result and anything else
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    __DEPLOY_INTERNALS__.setInterviewerEnabledForTests(undefined);
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(undefined);
    rmSync(tmp, { recursive: true, force: true });
  });

  test("disabled node reports failed and never reads or submits", async () => {
    __DEPLOY_INTERNALS__.setInterviewerEnabledForTests(false);
    await appendInterviewEvent({ role: "user", kind: "message", content: "secret window" });

    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-disabled",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 0 },
    });

    assert.equal(findRequest("/interview/submit"), undefined);
    const rp = resultPost();
    assert.ok(rp, "result POST expected");
    assert.equal(rp!.body!.status, "failed");
    assert.match(
      String((rp!.body!.result as Record<string, unknown>).error),
      /MEMCLAW_INTERVIEWER/,
    );
    // Buffer untouched.
    assert.equal((await readInterviewEvents(0, 500)).length, 1);
  });

  test("missing node_id reports failed", async () => {
    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-nonode",
      command: "interview_request",
      payload: { since_seq: 0 },
    });
    const rp = resultPost();
    assert.equal(rp!.body!.status, "failed");
    assert.match(String((rp!.body!.result as Record<string, unknown>).error), /node_id/);
  });

  test("empty buffer reports done with submitted:false and no submit call", async () => {
    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-empty",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 0 },
    });
    assert.equal(findRequest("/interview/submit"), undefined);
    const rp = resultPost();
    assert.equal(rp!.body!.status, "done");
    const result = rp!.body!.result as Record<string, unknown>;
    assert.equal(result.submitted, false);
  });

  test("happy path: submits the exact window, prunes through the committed watermark", async () => {
    for (let i = 0; i < 3; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `work item ${i}` });
    }
    submitResponse = {
      status: 200,
      body: { status: "committed", watermark: 2, memories_written: 3 },
    };

    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-happy",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 0, template_id: "default-v1" },
    });

    const submit = findRequest("/interview/submit");
    assert.ok(submit, "submit call expected");
    assert.equal(submit!.method, "POST");
    const sb = submit!.body!;
    assert.equal(sb.tenant_id, "t-test");
    assert.equal(sb.node_id, "node-uuid-1");
    assert.equal(sb.command_id, "cmd-happy");
    assert.equal(sb.cursor_from, 0);
    assert.equal(sb.cursor_to, 2);
    assert.equal((sb.events as unknown[]).length, 3);

    // Pruned through the committed watermark.
    assert.equal((await readInterviewEvents(0, 500)).length, 0);

    const rp = resultPost();
    assert.equal(rp!.body!.status, "done");
    const result = rp!.body!.result as Record<string, unknown>;
    assert.equal(result.submitted, true);
    assert.equal(result.watermark, 2);
    assert.equal(result.memories_written, 3);
  });

  test("submit reads from the server cursor, not from zero", async () => {
    for (let i = 0; i < 4; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `m${i}` });
    }
    submitResponse = { status: 200, body: { status: "committed", watermark: 3 } };

    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-cursor",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 2 },
    });

    const sb = findRequest("/interview/submit")!.body!;
    assert.equal(sb.cursor_from, 2);
    assert.equal(sb.cursor_to, 3);
    assert.equal((sb.events as unknown[]).length, 2);
  });

  test("failed submit (500) reports failed and does NOT prune the buffer", async () => {
    for (let i = 0; i < 3; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `keep me ${i}` });
    }
    submitResponse = { status: 500, body: { detail: "interview ingest failed" } };

    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-fail",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 0 },
    });

    const rp = resultPost();
    assert.equal(rp!.body!.status, "failed");
    // The no-loss invariant: nothing pruned on a failed window.
    assert.equal((await readInterviewEvents(0, 500)).length, 3);
  });

  test("2xx with missing watermark is a protocol error: failed, buffer preserved", async () => {
    for (let i = 0; i < 3; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `keep ${i}` });
    }
    // Misbehaving upstream: 200 OK but the body lacks a numeric watermark
    // (e.g. a proxy swallowed the JSON). Pruning here would silently drop
    // an uncommitted window.
    submitResponse = { status: 200, body: { ok: true } };

    await __DEPLOY_INTERNALS__.processCommand({
      id: "cmd-no-watermark",
      command: "interview_request",
      payload: { node_id: "node-uuid-1", since_seq: 0 },
    });

    const rp = resultPost();
    assert.equal(rp!.body!.status, "failed");
    assert.match(
      String((rp!.body!.result as Record<string, unknown>).error),
      /no numeric watermark/,
    );
    assert.equal((await readInterviewEvents(0, 500)).length, 3);
  });
});
