/**
 * Tests for the Interviewer's durable on-disk event buffer.
 *
 * The crash-safety story of Phase 1 rests on exactly the properties
 * pinned here: strictly monotonic seq that survives a process restart,
 * read-from-cursor, prune-only-through-committed, bounded field sizes,
 * and tolerance of a torn final line after a crash mid-append.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

import {
  appendInterviewEvent,
  readInterviewEvents,
  pruneInterviewBuffer,
  getInterviewBufferPath,
  __INTERVIEW_BUFFER_INTERNALS__,
} from "./interview-buffer.js";
import { INTERVIEW_EVENT_MAX_CHARS, INTERVIEW_FIELD_MAX_CHARS } from "./env.js";

describe("interview buffer", () => {
  let tmp: string;
  let bufPath: string;

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "memclaw-interview-buffer-"));
    bufPath = join(tmp, "interview-buffer.jsonl");
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath);
  });

  afterEach(() => {
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(undefined);
    rmSync(tmp, { recursive: true, force: true });
  });

  test("append assigns monotonic seqs and read returns them in order", async () => {
    const s0 = await appendInterviewEvent({ role: "user", kind: "message", content: "a" });
    const s1 = await appendInterviewEvent({ role: "assistant", kind: "message", content: "b" });
    const s2 = await appendInterviewEvent({ role: "user", kind: "message", content: "c" });
    assert.deepEqual([s0, s1, s2], [0, 1, 2]);

    const events = await readInterviewEvents(0, 500);
    assert.equal(events.length, 3);
    assert.deepEqual(
      events.map((e) => [e.seq, e.content]),
      [
        [0, "a"],
        [1, "b"],
        [2, "c"],
      ],
    );
  });

  test("read respects sinceSeq and maxCount", async () => {
    for (let i = 0; i < 5; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `m${i}` });
    }
    const fromTwo = await readInterviewEvents(2, 500);
    assert.deepEqual(
      fromTwo.map((e) => e.seq),
      [2, 3, 4],
    );
    const capped = await readInterviewEvents(0, 2);
    assert.deepEqual(
      capped.map((e) => e.seq),
      [0, 1],
    );
  });

  test("prune drops through committedSeq only, and is idempotent", async () => {
    for (let i = 0; i < 3; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `m${i}` });
    }
    await pruneInterviewBuffer(1);
    let events = await readInterviewEvents(0, 500);
    assert.deepEqual(
      events.map((e) => e.seq),
      [2],
    );
    await pruneInterviewBuffer(1); // no-op
    events = await readInterviewEvents(0, 500);
    assert.deepEqual(
      events.map((e) => e.seq),
      [2],
    );
  });

  test("seq survives a process restart (reseeded from the file tail)", async () => {
    await appendInterviewEvent({ role: "user", kind: "message", content: "before-a" });
    await appendInterviewEvent({ role: "user", kind: "message", content: "before-b" });

    // Simulate a gateway restart: module state reset, same file on disk.
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath);

    const next = await appendInterviewEvent({ role: "user", kind: "message", content: "after" });
    assert.equal(next, 2); // continues after the tail, no reuse and no gap
    const events = await readInterviewEvents(0, 500);
    assert.equal(events.length, 3);
  });

  test("prune-after-restart keeps the committed cursor semantics", async () => {
    for (let i = 0; i < 4; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `m${i}` });
    }
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath); // restart
    await pruneInterviewBuffer(2);
    const events = await readInterviewEvents(0, 500);
    assert.deepEqual(
      events.map((e) => e.seq),
      [3],
    );
    // Next append continues from the pre-prune tail, not from 0.
    const next = await appendInterviewEvent({ role: "user", kind: "message", content: "x" });
    assert.equal(next, 4);
  });

  test("caps content/tool/outcome to the server contract sizes", async () => {
    await appendInterviewEvent({
      role: "user",
      kind: "message",
      content: "x".repeat(INTERVIEW_EVENT_MAX_CHARS + 500),
      tool: "t".repeat(INTERVIEW_FIELD_MAX_CHARS + 50),
      outcome: "o".repeat(INTERVIEW_FIELD_MAX_CHARS + 50),
    });
    const [ev] = await readInterviewEvents(0, 1);
    assert.equal(ev.content.length, INTERVIEW_EVENT_MAX_CHARS);
    assert.equal(ev.tool!.length, INTERVIEW_FIELD_MAX_CHARS);
    assert.equal(ev.outcome!.length, INTERVIEW_FIELD_MAX_CHARS);
  });

  test("absent optional fields stay absent on the wire shape", async () => {
    await appendInterviewEvent({ role: "user", kind: "message", content: "plain" });
    const [ev] = await readInterviewEvents(0, 1);
    assert.ok(!("tool" in ev));
    assert.ok(!("outcome" in ev));
    assert.equal(ev.session_id, null);
  });

  test("tolerates a torn final line from a crash mid-append", async () => {
    await appendInterviewEvent({ role: "user", kind: "message", content: "good" });
    // Simulate a crash that tore the last write.
    const raw = readFileSync(bufPath, "utf-8");
    writeFileSync(bufPath, raw + '{"seq":1,"ts":"2026-07-17T', "utf-8");
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath); // restart

    const events = await readInterviewEvents(0, 500);
    assert.equal(events.length, 1); // torn line skipped
    const next = await appendInterviewEvent({ role: "user", kind: "message", content: "next" });
    assert.equal(next, 1); // reseeded from the last VALID line
  });

  test("buffer file is created lazily under the configured path", async () => {
    assert.equal(existsSync(bufPath), false);
    await appendInterviewEvent({ role: "user", kind: "message", content: "x" });
    assert.equal(existsSync(bufPath), true);
    assert.equal(getInterviewBufferPath(), bufPath);
  });
});

describe("interview buffer — wet-test regressions (task #6)", () => {
  let tmp: string;
  let bufPath: string;

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "memclaw-interview-wet-"));
    bufPath = join(tmp, "interview-buffer.jsonl");
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath);
  });

  afterEach(() => {
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(undefined);
    rmSync(tmp, { recursive: true, force: true });
  });

  test("seq does NOT reset after a full prune + restart (meta sidecar)", async () => {
    // VM wet-test finding #1: prune-to-empty + gateway restart reseeded
    // seq at 0 — below the server watermark, hiding all new events.
    for (let i = 0; i < 3; i++) {
      await appendInterviewEvent({ role: "user", kind: "message", content: `m${i}` });
    }
    await pruneInterviewBuffer(2); // buffer now EMPTY
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath); // restart
    const next = await appendInterviewEvent({ role: "user", kind: "message", content: "after" });
    assert.equal(next, 3); // continues past the pruned range, not 0
  });

  test("torn line WITHOUT trailing newline does not swallow the next append", async () => {
    // VM wet-test finding #2: a crash mid-append leaves a partial line
    // with no newline; the next append must not concatenate onto it.
    await appendInterviewEvent({ role: "user", kind: "message", content: "good" });
    const raw = readFileSync(bufPath, "utf-8");
    writeFileSync(bufPath, raw + '{"seq":1,"ts":"2026-07-17T', "utf-8"); // no \n
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(bufPath); // restart

    const a = await appendInterviewEvent({ role: "user", kind: "message", content: "next-1" });
    const b = await appendInterviewEvent({ role: "user", kind: "message", content: "next-2" });
    const events = await readInterviewEvents(0, 500);
    // good + next-1 + next-2 all survive; the torn fragment is skipped.
    assert.equal(events.length, 3);
    assert.deepEqual(
      events.map((e) => e.content),
      ["good", "next-1", "next-2"],
    );
    assert.deepEqual([a, b], [1, 2]);
  });
});
