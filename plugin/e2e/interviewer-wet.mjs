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
  } else {
    throw new Error(`unknown subcommand: ${cmd}`);
  }
  process.exit(0);
} catch (e) {
  out({ ok: false, error: String(e && e.message ? e.message : e) });
  process.exit(1);
}
