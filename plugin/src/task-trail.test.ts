/**
 * Tests for the Interviewer's OpenClaw task-trail capture (#654).
 *
 * Pins the properties the fix rests on:
 * - DB discovery across BOTH OpenClaw layouts (2026.4/5 standalone
 *   ``tasks/runs.sqlite``; >= 2026.6 consolidated state DB with an
 *   unstable filename) plus the operator env override;
 * - column probing: an old schema missing the optional summary columns
 *   still syncs (falls back to task/status), a schema missing REQUIRED
 *   columns degrades to task-db-unavailable instead of throwing;
 * - the delta contract: at most two events per task (discovered +
 *   terminal), progress-only bumps emit nothing, re-syncs are no-ops;
 * - fail-soft: no DB present is a note, never a throw;
 * - the per-tick cap with natural resume on the next sync;
 * - sidecar pruning past the retention horizon.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, readFileSync, writeFileSync, existsSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";
import { DatabaseSync } from "node:sqlite";

const { syncTaskTrail, __TASK_TRAIL_INTERNALS__ } = await import("./task-trail.js");
const { readInterviewEvents, __INTERVIEW_BUFFER_INTERNALS__ } = await import(
  "./interview-buffer.js"
);
const { INTERVIEW_TASK_SYNC_MAX_PER_TICK } = await import("./env.js");

/** The real task_runs schema (captured from an OpenClaw 2026.4/5 install). */
const FULL_SCHEMA = `
  CREATE TABLE task_runs (
    task_id TEXT PRIMARY KEY,
    runtime TEXT NOT NULL,
    task_kind TEXT,
    source_id TEXT,
    requester_session_key TEXT,
    owner_key TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    child_session_key TEXT,
    parent_flow_id TEXT,
    parent_task_id TEXT,
    agent_id TEXT,
    run_id TEXT,
    label TEXT,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    notify_policy TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    last_event_at INTEGER,
    cleanup_after INTEGER,
    error TEXT,
    progress_summary TEXT,
    terminal_summary TEXT,
    terminal_outcome TEXT
  );
`;

interface RowSpec {
  task_id: string;
  task?: string;
  status?: string;
  runtime?: string;
  label?: string | null;
  created_at?: number;
  last_event_at?: number | null;
  ended_at?: number | null;
  terminal_summary?: string | null;
  progress_summary?: string | null;
  terminal_outcome?: string | null;
  error?: string | null;
  child_session_key?: string | null;
}

function makeDb(path: string, schema: string = FULL_SCHEMA): void {
  const db = new DatabaseSync(path);
  db.exec(schema);
  db.close();
}

function insertRow(path: string, spec: RowSpec): void {
  const db = new DatabaseSync(path);
  const now = Date.now();
  db.prepare(
    `INSERT OR REPLACE INTO task_runs
     (task_id, runtime, owner_key, scope_kind, task, status, delivery_status,
      notify_policy, created_at, last_event_at, ended_at, label,
      terminal_summary, progress_summary, terminal_outcome, error, child_session_key)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    spec.task_id,
    spec.runtime ?? "subagent",
    "owner",
    "session",
    spec.task ?? "investigate the flaky deploy",
    spec.status ?? "running",
    "none",
    "auto",
    spec.created_at ?? now,
    spec.last_event_at === undefined ? now : spec.last_event_at,
    spec.ended_at ?? null,
    spec.label ?? null,
    spec.terminal_summary ?? null,
    spec.progress_summary ?? null,
    spec.terminal_outcome ?? null,
    spec.error ?? null,
    spec.child_session_key ?? null,
  );
  db.close();
}

describe("task-trail sync", () => {
  let tmp: string;
  let legacyDbPath: string;

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "memclaw-task-trail-"));
    __TASK_TRAIL_INTERNALS__.setBaseDirForTests(tmp);
    __TASK_TRAIL_INTERNALS__.setSidecarPathForTests(join(tmp, "sidecar.json"));
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(join(tmp, "buf.jsonl"));
    legacyDbPath = join(tmp, "tasks", "runs.sqlite");
    mkdirSync(join(tmp, "tasks"), { recursive: true });
  });

  afterEach(() => {
    __TASK_TRAIL_INTERNALS__.setBaseDirForTests(undefined);
    __TASK_TRAIL_INTERNALS__.setSidecarPathForTests(undefined);
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(undefined);
    rmSync(tmp, { recursive: true, force: true });
  });

  test("no task DB anywhere degrades to a note, never a throw", async () => {
    const res = await syncTaskTrail();
    assert.equal(res.synced, 0);
    assert.match(res.note ?? "", /task-db-unavailable/);
    const events = await readInterviewEvents(0, 100);
    assert.equal(events.length, 0);
  });

  test("MEMCLAW_TASK_DB_PATH override: valid path is exclusive, broken path is named in the note", async () => {
    // Valid override: used exclusively, even though a legacy DB also exists.
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t-legacy" });
    const overridePath = join(tmp, "custom-tasks.sqlite");
    makeDb(overridePath);
    insertRow(overridePath, { task_id: "t-override", task: "work in the override DB" });
    __TASK_TRAIL_INTERNALS__.setTaskDbPathForTests(overridePath);
    try {
      const res = await syncTaskTrail();
      assert.equal(res.synced, 1);
      assert.match((await readInterviewEvents(0, 100))[0].content, /override DB/);

      // Broken override: the note must NAME the misconfigured path instead
      // of the generic "not found" (which reads as an idle node).
      __TASK_TRAIL_INTERNALS__.setTaskDbPathForTests(join(tmp, "does-not-exist.sqlite"));
      const broken = await syncTaskTrail();
      assert.equal(broken.synced, 0);
      assert.match(broken.note ?? "", /MEMCLAW_TASK_DB_PATH=.*does-not-exist\.sqlite/);
    } finally {
      __TASK_TRAIL_INTERNALS__.setTaskDbPathForTests(undefined);
    }
  });

  test("legacy layout: new running task emits ONE discovered event", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, {
      task_id: "t1",
      task: "compile the weekly revenue digest",
      runtime: "subagent",
      label: "digest",
      child_session_key: "agent:main:sub:42",
    });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 1);
    assert.equal(res.note, undefined);
    const events = await readInterviewEvents(0, 100);
    assert.equal(events.length, 1);
    assert.equal(events[0].kind, "task");
    assert.equal(events[0].role, "agent");
    assert.equal(events[0].session_id, "agent:main:sub:42");
    assert.equal(events[0].tool, "subagent");
    assert.match(events[0].content, /\[task subagent\/digest\] compile the weekly/);
  });

  test("terminal transition emits task_result with summary + outcome", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t1", status: "running" });
    await syncTaskTrail(); // discovers t1
    insertRow(legacyDbPath, {
      task_id: "t1",
      status: "completed",
      ended_at: Date.now(),
      terminal_summary: "digest built and posted to #revenue",
      terminal_outcome: "ok",
    });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 1);
    const events = await readInterviewEvents(0, 100);
    assert.equal(events.length, 2);
    assert.equal(events[1].kind, "task_result");
    assert.match(events[1].content, /digest built and posted/);
    assert.equal(events[1].outcome, "ok");
  });

  test("progress-only bumps emit nothing; re-sync is a no-op", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t1", status: "running" });
    await syncTaskTrail();
    insertRow(legacyDbPath, {
      task_id: "t1",
      status: "running",
      last_event_at: Date.now() + 1000,
      progress_summary: "half done",
    });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 0);
    const res2 = await syncTaskTrail();
    assert.equal(res2.synced, 0);
    assert.equal((await readInterviewEvents(0, 100)).length, 1);
  });

  test("task that appears already-terminal emits both halves in one sync", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, {
      task_id: "t1",
      status: "failed",
      ended_at: Date.now(),
      error: "npm install exploded",
    });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 2);
    const events = await readInterviewEvents(0, 100);
    assert.equal(events[0].kind, "task");
    assert.equal(events[1].kind, "task_result");
    assert.match(events[1].content, /npm install exploded/);
    assert.equal(events[1].outcome, "failed"); // no terminal_outcome -> status
  });

  test("old schema without summary/label columns still syncs (fallback)", async () => {
    makeDb(
      legacyDbPath,
      `CREATE TABLE task_runs (
        task_id TEXT PRIMARY KEY,
        task TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        ended_at INTEGER
      );`,
    );
    const db = new DatabaseSync(legacyDbPath);
    db.prepare(
      "INSERT INTO task_runs (task_id, task, status, created_at, ended_at) VALUES (?, ?, ?, ?, ?)",
    ).run("t-old", "ancient work", "completed", Date.now(), Date.now());
    db.close();
    const res = await syncTaskTrail();
    assert.equal(res.synced, 2);
    const events = await readInterviewEvents(0, 100);
    assert.match(events[0].content, /\[task\] ancient work/);
    assert.match(events[1].content, /task ended: completed/); // summary fallback
    assert.equal(events[1].outcome, "completed");
  });

  test("schema missing REQUIRED columns degrades, never throws", async () => {
    makeDb(legacyDbPath, "CREATE TABLE task_runs (task_id TEXT PRIMARY KEY);");
    const res = await syncTaskTrail();
    assert.equal(res.synced, 0);
    assert.match(res.note ?? "", /task-db-unavailable: no readable task_runs schema/);
  });

  test("consolidated layout: scan finds task_runs DB, skips decoys", async () => {
    // Decoy: a memory-index sqlite WITHOUT task_runs (real OpenClaw layout).
    mkdirSync(join(tmp, "memory"), { recursive: true });
    const decoy = new DatabaseSync(join(tmp, "memory", "agent.sqlite"));
    decoy.exec("CREATE TABLE chunks (id TEXT PRIMARY KEY);");
    decoy.close();
    // The consolidated DB, deliberately NOT at the legacy path/name.
    const statePath = join(tmp, "openclaw-state.sqlite");
    makeDb(statePath);
    insertRow(statePath, { task_id: "t-consolidated" });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 1);
    // Synced paths are recorded in the sidecar (informational, not a cache).
    const sidecar = JSON.parse(readFileSync(join(tmp, "sidecar.json"), "utf-8"));
    assert.deepEqual(sidecar.db_paths, [statePath]);
  });

  test("stale post-upgrade legacy DB cannot shadow the live consolidated DB", async () => {
    // The 2026.5 -> 2026.6 upgrade scenario: the old tasks/runs.sqlite
    // survives with a valid-but-frozen task_runs table (and was even the
    // previously-synced source), while all NEW tasks land in the
    // consolidated state DB. Both must be read — single-winner discovery
    // would return the frozen file forever and report synced:0,
    // indistinguishable from an idle node.
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t-pre-upgrade" });
    const first = await syncTaskTrail(); // sidecar now knows the legacy path
    assert.equal(first.synced, 1);

    const statePath = join(tmp, "openclaw-state.sqlite");
    makeDb(statePath);
    insertRow(statePath, {
      task_id: "t-post-upgrade",
      task: "work recorded only in the consolidated DB",
    });
    const second = await syncTaskTrail();
    assert.equal(second.synced, 1); // the new task IS captured
    const events = await readInterviewEvents(0, 100);
    assert.match(events[1].content, /consolidated DB/);
    // Migrated duplicate of the old task in the new DB de-dups via sidecar.
    insertRow(statePath, { task_id: "t-pre-upgrade" });
    const third = await syncTaskTrail();
    assert.equal(third.synced, 0);
  });

  test("per-tick cap: remainder drains on the next sync", async () => {
    makeDb(legacyDbPath);
    const total = INTERVIEW_TASK_SYNC_MAX_PER_TICK + 25;
    const base = Date.now() - 60_000;
    for (let i = 0; i < total; i++) {
      insertRow(legacyDbPath, {
        task_id: `t${String(i).padStart(4, "0")}`,
        last_event_at: base + i, // stable ordering
      });
    }
    const first = await syncTaskTrail();
    assert.equal(first.synced, INTERVIEW_TASK_SYNC_MAX_PER_TICK);
    assert.match(first.note ?? "", /capped/);
    const second = await syncTaskTrail();
    assert.equal(second.synced, 25);
    assert.equal(second.note, undefined);
    assert.equal((await readInterviewEvents(0, 1000)).length, total);
  });

  test("terminal event deferred by the cap is emitted on the NEXT tick, not lost", async () => {
    makeDb(legacyDbPath);
    const base = Date.now() - 60_000;
    // Fill the tick to exactly one slot below the cap...
    for (let i = 0; i < INTERVIEW_TASK_SYNC_MAX_PER_TICK - 1; i++) {
      insertRow(legacyDbPath, {
        task_id: `t${String(i).padStart(4, "0")}`,
        last_event_at: base + i,
      });
    }
    // ...so this already-terminal task gets its discovered event as the
    // cap-th emission and its terminal half deferred.
    insertRow(legacyDbPath, {
      task_id: "t-boundary",
      status: "completed",
      last_event_at: base + 100_000,
      ended_at: Date.now(),
      terminal_summary: "finished at the cap boundary",
      terminal_outcome: "ok",
    });
    const first = await syncTaskTrail();
    assert.equal(first.synced, INTERVIEW_TASK_SYNC_MAX_PER_TICK);
    assert.match(first.note ?? "", /capped/);
    // The sidecar must NOT have recorded the terminal half as emitted.
    const second = await syncTaskTrail();
    assert.equal(second.synced, 1);
    const events = await readInterviewEvents(0, 1000);
    const last = events[events.length - 1];
    assert.equal(last.kind, "task_result");
    assert.match(last.content, /finished at the cap boundary/);
    assert.equal(last.outcome, "ok");
  });

  test("sidecar prunes entries past the retention horizon", async () => {
    makeDb(legacyDbPath);
    const stale = Date.now() - 9 * 24 * 60 * 60_000; // > 8d retention
    writeFileSync(
      join(tmp, "sidecar.json"),
      JSON.stringify({
        tasks: { "t-ancient": { status: "completed", last_event_at: stale, terminal_emitted: true } },
      }),
      "utf-8",
    );
    insertRow(legacyDbPath, { task_id: "t-fresh" });
    await syncTaskTrail();
    const sidecar = JSON.parse(readFileSync(join(tmp, "sidecar.json"), "utf-8"));
    assert.equal(sidecar.tasks["t-ancient"], undefined);
    assert.ok(sidecar.tasks["t-fresh"]);
  });

  test("merged multi-DB rows are appended in global chronological order", async () => {
    // Interleaved timestamps across two DBs: legacy holds t=1000 and
    // t=3000, consolidated holds t=2000. Without the global re-sort the
    // buffer (and any cap deferral) would follow DB-discovery order.
    const base = Date.now() - 60_000;
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t-a", task: "first", last_event_at: base + 1000 });
    insertRow(legacyDbPath, { task_id: "t-c", task: "third", last_event_at: base + 3000 });
    const statePath = join(tmp, "openclaw-state.sqlite");
    makeDb(statePath);
    insertRow(statePath, { task_id: "t-b", task: "second", last_event_at: base + 2000 });
    const res = await syncTaskTrail();
    assert.equal(res.synced, 3);
    const events = await readInterviewEvents(0, 100);
    assert.match(events[0].content, /first/);
    assert.match(events[1].content, /second/);
    assert.match(events[2].content, /third/);
  });

  test("buffer failure BEFORE the first append reports task-trail-error, not db-unavailable", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t1" });
    // Break the BUFFER (not the DB): its parent path is a file, so the very
    // first appendInterviewEvent throws — appended stays 0, but the DB read
    // succeeded, so blaming the task DB would send operators the wrong way.
    writeFileSync(join(tmp, "bufblocker"), "not a directory", "utf-8");
    __INTERVIEW_BUFFER_INTERNALS__.setPathForTests(join(tmp, "bufblocker", "buf.jsonl"));
    const res = await syncTaskTrail();
    assert.equal(res.synced, 0);
    assert.match(res.note ?? "", /task-trail-error/);
    assert.doesNotMatch(res.note ?? "", /task-db-unavailable/);
  });

  test("failure AFTER appends reports partial-sync-error with the real count", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t1" });
    // Make saveSidecar fail deterministically: its parent path is a FILE,
    // so mkdir -p throws (ENOTDIR) — but only after the appends succeeded.
    writeFileSync(join(tmp, "blocker"), "not a directory", "utf-8");
    __TASK_TRAIL_INTERNALS__.setSidecarPathForTests(join(tmp, "blocker", "sidecar.json"));
    const res = await syncTaskTrail();
    assert.equal(res.synced, 1); // the appended event is real and reported
    assert.match(res.note ?? "", /partial-sync-error/); // NOT task-db-unavailable
    assert.equal((await readInterviewEvents(0, 100)).length, 1);
  });

  test("crash-torn sidecar starts fresh instead of throwing", async () => {
    makeDb(legacyDbPath);
    insertRow(legacyDbPath, { task_id: "t1" });
    writeFileSync(join(tmp, "sidecar.json"), '{"tasks": {"t1"', "utf-8"); // torn JSON
    const res = await syncTaskTrail();
    assert.equal(res.synced, 1); // treated as unseen; re-emission is the accepted cost
    assert.ok(existsSync(join(tmp, "sidecar.json")));
  });
});
