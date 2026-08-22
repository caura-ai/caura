#!/usr/bin/env node
/**
 * Interviewer Phase 1 — wet-test harness (task #6).
 *
 * Drives the REAL compiled plugin modules (dist/heartbeat.js —
 * sendHeartbeat + processCommand — and dist/interview-buffer.js) against
 * a REAL backend stack, replacing only the OpenClaw gateway wrapper.
 * Each subcommand prints a single JSON line on stdout for the
 * orchestrating shell script (interviewer-wet.sh) to assert on.
 *
 * Required env (set by the orchestrator): MEMCLAW_API_URL,
 * MEMCLAW_API_KEY (admin), MEMCLAW_TENANT_ID, MEMCLAW_FLEET_ID,
 * MEMCLAW_NODE_NAME, MEMCLAW_INTERVIEWER=true.
 */

const API = process.env.MEMCLAW_API_URL || "http://localhost:8000";
const KEY = process.env.MEMCLAW_API_KEY || "";
const TENANT = process.env.MEMCLAW_TENANT_ID || "";
const NODE_NAME = process.env.MEMCLAW_NODE_NAME || "";

function out(obj) {
  console.log(JSON.stringify(obj));
}

async function api(method, path, body, query) {
  const url = new URL(`/api/v1${path}`, API);
  for (const [k, v] of Object.entries(query || {})) url.searchParams.set(k, v);
  const res = await fetch(url, {
    method,
    headers: { "X-API-Key": KEY, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text.slice(0, 300) };
  }
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${text.slice(0, 200)}`);
  return data;
}

async function nodeUuid() {
  const nodes = await api("GET", "/fleet/nodes", undefined, { tenant_id: TENANT });
  const mine = nodes.find((n) => n.node_name === NODE_NAME);
  if (!mine) throw new Error(`node ${NODE_NAME} not registered`);
  return mine.node_id;
}

const cmd = process.argv[2];
const arg1 = process.argv[3];
const arg2 = process.argv[4];
const arg3 = process.argv[5];

try {
  if (cmd === "set-enabled") {
    // arg1: "true" | "false"
    await api("PUT", "/settings", { interviewer: { enabled: arg1 === "true" } }, { tenant_id: TENANT });
    out({ ok: true, enabled: arg1 === "true" });
  } else if (cmd === "heartbeat") {
    const { sendHeartbeat } = await import("../dist/heartbeat.js");
    await sendHeartbeat(); // registers node + pulls + processes pending commands
    out({ ok: true });
  } else if (cmd === "append") {
    // arg1: count, arg2: label
    const { appendInterviewEvent } = await import("../dist/interview-buffer.js");
    const n = parseInt(arg1 || "1", 10);
    let first = -1;
    let last = -1;
    for (let i = 0; i < n; i++) {
      const seq = await appendInterviewEvent({
        session_id: "wet-session",
        role: i % 2 ? "assistant" : "user",
        kind: "message",
        content: `[${arg2 || "wet"}] event ${i}: worked on the interviewer wet test, step ${i}.`,
      });
      if (first < 0) first = seq;
      last = seq;
    }
    out({ ok: true, appended: n, first_seq: first, last_seq: last });
  } else if (cmd === "append-loop") {
    // Endless append for the kill -9 test. Prints one line per append.
    const { appendInterviewEvent } = await import("../dist/interview-buffer.js");
    let i = 0;
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const seq = await appendInterviewEvent({
        session_id: "wet-killer",
        role: "user",
        kind: "message",
        content: `kill-test event ${i++}`,
      });
      out({ seq });
      await new Promise((r) => setTimeout(r, 20));
    }
  } else if (cmd === "count") {
    const { readInterviewEvents } = await import("../dist/interview-buffer.js");
    const events = await readInterviewEvents(0, 1_000_000);
    const seqs = events.map((e) => e.seq);
    const monotonic = seqs.every((s, i) => i === 0 || s > seqs[i - 1]);
    out({
      count: events.length,
      first_seq: seqs[0] ?? null,
      last_seq: seqs[seqs.length - 1] ?? null,
      strictly_monotonic: monotonic,
    });
  } else if (cmd === "schedule") {
    const summary = await api("POST", "/admin/interview/schedule/run");
    out(summary);
  } else if (cmd === "queue-cmd") {
    // arg1: since_seq — queue an interview_request directly on the rail
    // (used to force runs without waiting out the dueness period).
    const uuid = await nodeUuid();
    const created = await api("POST", "/fleet/commands", {
      tenant_id: TENANT,
      node_id: uuid,
      command: "interview_request",
      payload: {
        node_id: uuid,
        since_seq: parseInt(arg1 || "0", 10),
        template_id: "default-v1",
        period_hours: 12,
      },
    });
    out({ ok: true, command_id: created.id, node_uuid: uuid });
  } else if (cmd === "commands") {
    const rows = await api("GET", "/fleet/commands", undefined, { tenant_id: TENANT });
    const ours = rows.filter((c) => c.command === "interview_request");
    out({
      total: ours.length,
      latest: ours[0]
        ? { id: ours[0].id, status: ours[0].status, result: ours[0].result }
        : null,
    });
  } else if (cmd === "memories") {
    const data = await api("GET", "/memories", undefined, {
      tenant_id: TENANT,
      limit: "200",
    });
    const rows = Array.isArray(data) ? data : data.items || [];
    const ours = rows.filter((r) => (r.metadata || {}).source === "interviewer");
    out({ interviewer_memories: ours.length, types: ours.map((r) => r.memory_type).sort() });
  } else if (cmd === "get-enabled") {
    const s = await api("GET", "/settings", undefined, { tenant_id: TENANT });
    out({ enabled: !!(s.interviewer || {}).enabled });
  } else if (cmd === "node-uuid") {
    out({ node_uuid: await nodeUuid() });

    // ------------------------------------------------------------------
    // Phase 1.5 (task-trail, #654) — capture-layer subcommands. Unlike
    // "append" (which hand-feeds the buffer and therefore validates only
    // the pipeline), these operate on the OpenClaw task_runs DB so the
    // REAL capture path (discovery -> probe -> delta -> buffer) is what
    // gets exercised.
    // ------------------------------------------------------------------
  } else if (cmd === "sync") {
    // Run the real task-trail sync directly; print its result AND the
    // sidecar (db_paths records which DBs discovery actually found — on
    // >= 2026.6 this documents the consolidated DB's real filename).
    const { syncTaskTrail } = await import("../dist/task-trail.js");
    const res = await syncTaskTrail();
    let sidecar = null;
    try {
      const { readFileSync } = await import("fs");
      const { join } = await import("path");
      const { homedir } = await import("os");
      sidecar = JSON.parse(
        readFileSync(
          join(homedir(), ".openclaw", "plugins", "memclaw", "interview-task-sync.json"),
          "utf-8",
        ),
      );
    } catch {
      // sidecar may not exist yet
    }
    out({ ...res, db_paths: sidecar?.db_paths ?? null, tracked_tasks: sidecar ? Object.keys(sidecar.tasks).length : 0 });
  } else if (cmd === "task-insert") {
    // arg1: db path, arg2: count, arg3: label, argv[6]: "terminal" to
    // insert already-finished rows. Schema-INTROSPECTING: reads the real
    // table's columns via PRAGMA and only fills what exists, so the same
    // command works against whatever schema the installed OpenClaw
    // version created — rows are as real as the schema is.
    const { DatabaseSync } = await import("node:sqlite");
    const db = new DatabaseSync(arg1);
    const cols = db.prepare("PRAGMA table_info(task_runs)").all();
    const names = new Set(cols.map((c) => c.name));
    const notNull = cols.filter((c) => c.notnull && c.dflt_value === null && c.name !== "task_id");
    const n = parseInt(arg2 || "1", 10);
    const label = arg3 || "wet";
    const terminal = process.argv[6] === "terminal";
    const now = Date.now();
    const ids = [];
    for (let i = 0; i < n; i++) {
      const id = `wet-${label}-${now}-${i}`;
      const row = { task_id: id };
      // Known semantic columns first.
      if (names.has("task")) row.task = `[${label}] investigate wet-test workload item ${i}`;
      if (names.has("status")) row.status = terminal ? "completed" : "running";
      if (names.has("runtime")) row.runtime = "subagent";
      if (names.has("label")) row.label = label;
      if (names.has("created_at")) row.created_at = now - 10_000 + i;
      if (names.has("last_event_at")) row.last_event_at = now - 5_000 + i;
      if (terminal) {
        if (names.has("ended_at")) row.ended_at = now - 1_000 + i;
        if (names.has("terminal_summary"))
          row.terminal_summary = `completed wet workload ${i}: validated the ${label} path end-to-end`;
        if (names.has("terminal_outcome")) row.terminal_outcome = "ok";
      }
      // Any remaining NOT-NULL-without-default column gets a type-shaped
      // filler so the insert works on schema variants we haven't seen.
      for (const c of notNull) {
        if (row[c.name] !== undefined) continue;
        row[c.name] = /INT|REAL|NUM/i.test(c.type || "") ? 0 : "wet";
      }
      const keys = Object.keys(row);
      db.prepare(
        `INSERT OR REPLACE INTO task_runs (${keys.join(",")}) VALUES (${keys.map(() => "?").join(",")})`,
      ).run(...keys.map((k) => row[k]));
      ids.push(id);
    }
    db.close();
    out({ ok: true, inserted: n, terminal, first_id: ids[0], db: arg1 });
  } else if (cmd === "task-finish") {
    // arg1: db path, arg2: task_id, arg3: summary, argv[6]: outcome
    const { DatabaseSync } = await import("node:sqlite");
    const db = new DatabaseSync(arg1);
    const names = new Set(db.prepare("PRAGMA table_info(task_runs)").all().map((c) => c.name));
    const sets = ["status = 'completed'"];
    const vals = [];
    if (names.has("ended_at")) sets.push(`ended_at = ${Date.now()}`);
    if (names.has("last_event_at")) sets.push(`last_event_at = ${Date.now()}`);
    if (names.has("terminal_summary")) {
      sets.push("terminal_summary = ?");
      vals.push(arg3 || "finished");
    }
    if (names.has("terminal_outcome")) {
      sets.push("terminal_outcome = ?");
      vals.push(process.argv[6] || "ok");
    }
    db.prepare(`UPDATE task_runs SET ${sets.join(", ")} WHERE task_id = ?`).run(...vals, arg2);
    db.close();
    out({ ok: true, task_id: arg2 });
  } else if (cmd === "task-db-schema") {
    // arg1: db path — print the REAL table columns (documents what the
    // installed OpenClaw version actually ships; phase-B evidence).
    const { DatabaseSync } = await import("node:sqlite");
    const db = new DatabaseSync(arg1, { readOnly: true });
    const cols = db.prepare("PRAGMA table_info(task_runs)").all().map((c) => c.name);
    const count = db.prepare("SELECT COUNT(*) AS c FROM task_runs").get().c;
    db.close();
    out({ ok: true, db: arg1, columns: cols, rows: count });
  } else if (cmd === "tick") {
    // arg1: since_seq — one full interview tick: queue command on the
    // rail, heartbeat to process it, return the latest command result
    // (incl. synced_tasks / task_trail — the phase-1.5 observability).
    const uuid = await nodeUuid();
    await api("POST", "/fleet/commands", {
      tenant_id: TENANT,
      node_id: uuid,
      command: "interview_request",
      payload: { node_id: uuid, since_seq: parseInt(arg1 || "0", 10), template_id: "default-v1", period_hours: 12 },
    });
    const { sendHeartbeat } = await import("../dist/heartbeat.js");
    await sendHeartbeat();
    const rows = await api("GET", "/fleet/commands", undefined, { tenant_id: TENANT });
    const ours = rows.filter((c) => c.command === "interview_request");
    out({ ok: true, status: ours[0]?.status, result: ours[0]?.result ?? null });
  } else {
    throw new Error(`unknown subcommand: ${cmd}`);
  }
  process.exit(0);
} catch (e) {
  out({ ok: false, error: String(e && e.message ? e.message : e) });
  process.exit(1);
}
