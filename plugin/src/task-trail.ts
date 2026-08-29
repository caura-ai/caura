/**
 * Interviewer Phase 1.5 — OpenClaw task-trail capture (issue #654).
 *
 * The Phase-1 buffer captured only the host message stream
 * (``context-engine.ingest()``), so agents whose work runs through
 * spawned tasks / sub-agents — recorded in OpenClaw's ``task_runs``
 * SQLite, never in the chat stream — produced empty interviews forever.
 *
 * This module mirrors the agent's durable task trail into the SAME
 * interview buffer at interview time (no background poller): the
 * ``interview_request`` handler calls ``syncTaskTrail()`` before it
 * reads the window, so "buffer empty after sync" genuinely means idle.
 * The buffer's seq / prune / watermark machinery and the frozen server
 * contract are untouched — task rows just become C2 events.
 *
 * Version tolerance (OpenClaw >= 2026.4, verified against source):
 * - 2026.4–2026.5: standalone ``<base>/tasks/runs.sqlite``
 *   (``resolveTaskRegistrySqlitePath``);
 * - >= 2026.6: store refactor — ``task_runs`` lives in a consolidated
 *   state DB whose filename is not stable across versions.
 * So the DBs are DISCOVERED (env override, else legacy path + shallow
 * scan for ``task_runs`` tables) and ALL of them are synced — a frozen
 * post-upgrade leftover can't shadow the live DB when there is no
 * single winner. Columns are PROBED via PRAGMA — the
 * reader degrades to the minimum column set instead of breaking on
 * schema drift. ``node:sqlite`` ships with every Node able to run
 * OpenClaw >= 2026.4 (OpenClaw itself uses ``DatabaseSync``), read-only
 * over WAL, so we never contend with the gateway's own writes.
 *
 * Everything here is fail-soft: any error degrades to
 * ``{synced: 0, note: "task-db-unavailable: ..."}`` — a tick must never
 * fail because the task DB is missing, locked, or reshaped.
 */

import { existsSync, readFileSync, readdirSync, statSync } from "fs";
import { writeFile, mkdir, rename } from "fs/promises";
import { dirname, join } from "path";

import { appendInterviewEvent } from "./interview-buffer.js";
import { getOpenClawBaseDir, getPluginDir } from "./paths.js";
import {
  INTERVIEW_TASK_SYNC_MAX_PER_TICK,
  INTERVIEW_TASK_SIDECAR_RETENTION_MS,
  CAURA_TASK_DB_PATH,
} from "./env.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TaskSyncResult {
  /** Events appended to the interview buffer this sync. */
  synced: number;
  /** Present when the sync degraded (db unavailable, cap hit, ...). */
  note?: string;
}

interface SidecarTaskState {
  /** Last status we saw for the task (informational). */
  status: string;
  /** Row's last_event_at at last emission — drives sidecar pruning. */
  last_event_at: number;
  /** Whether the terminal task_result event was already emitted. */
  terminal_emitted: boolean;
}

interface SidecarState {
  /**
   * Paths synced on the last tick — informational/debug only. NOT a
   * discovery cache: caching a winner is what made a stale post-upgrade
   * DB shadow the live one (see discoverTaskDbs).
   */
  db_paths?: string[];
  tasks: Record<string, SidecarTaskState>;
}

interface TaskRow {
  task_id: string;
  status: string;
  created_at: number;
  last_event_at: number | null;
  ended_at: number | null;
  task: string;
  label: string | null;
  runtime: string | null;
  agent_id: string | null;
  progress_summary: string | null;
  terminal_summary: string | null;
  terminal_outcome: string | null;
  error: string | null;
  child_session_key: string | null;
  requester_session_key: string | null;
}

// Minimum columns the reader needs; everything else is optional and
// probed. ``last_event_at`` is technically optional too (falls back to
// created_at) so a very old schema still syncs.
const REQUIRED_COLUMNS = ["task_id", "status", "created_at", "task"] as const;
const OPTIONAL_COLUMNS = [
  "last_event_at",
  "ended_at",
  "label",
  "runtime",
  "agent_id",
  "progress_summary",
  "terminal_summary",
  "terminal_outcome",
  "error",
  "child_session_key",
  "requester_session_key",
] as const;

// ---------------------------------------------------------------------------
// Test overrides
// ---------------------------------------------------------------------------

let _baseDirOverride: string | undefined;
let _sidecarPathOverride: string | undefined;
let _taskDbPathOverride: string | undefined;

export const __TASK_TRAIL_INTERNALS__ = {
  setBaseDirForTests(dir: string | undefined): void {
    _baseDirOverride = dir;
  },
  setSidecarPathForTests(path: string | undefined): void {
    _sidecarPathOverride = path;
  },
  setTaskDbPathForTests(path: string | undefined): void {
    _taskDbPathOverride = path;
  },
};

function taskDbPathOverride(): string {
  return _taskDbPathOverride ?? CAURA_TASK_DB_PATH;
}

function baseDir(): string {
  return _baseDirOverride ?? getOpenClawBaseDir();
}

/**
 * ~/.openclaw/plugins/memclaw/interview-task-sync.json // legacy-name-floor: frozen install path
 *
 * Deliberately a shared singleton for the whole install: if two gateway
 * processes ever sync concurrently, both load the same sidecar and the
 * later ``saveSidecar`` wins — the loser re-emits already-seen events on
 * its next tick. That duplicate narrative is acceptable (the interview
 * LLM absorbs it; the seq/watermark contract is unaffected), so do NOT
 * "fix" this with file locking — a lock adds a blocking failure mode to
 * a path that must stay fail-soft, for a race whose cost is only dupes.
 */
function sidecarPath(): string {
  return _sidecarPathOverride ?? join(getPluginDir(), "interview-task-sync.json");
}

// ---------------------------------------------------------------------------
// Sidecar state
// ---------------------------------------------------------------------------

function loadSidecar(): SidecarState {
  try {
    const raw = readFileSync(sidecarPath(), "utf-8");
    const parsed = JSON.parse(raw) as SidecarState;
    if (parsed && typeof parsed === "object" && parsed.tasks && typeof parsed.tasks === "object") {
      return parsed;
    }
  } catch {
    // Missing or corrupt sidecar: start fresh. Worst case we re-emit
    // events for tasks still inside the DB's 7-day retention — duplicate
    // narrative the interview LLM absorbs, never data loss.
  }
  return { tasks: {} };
}

async function saveSidecar(state: SidecarState): Promise<void> {
  const path = sidecarPath();
  await mkdir(dirname(path), { recursive: true });
  // Write-then-rename so a crash mid-write can't leave a torn JSON that
  // would reset the delta state (and re-emit the whole window).
  const tmp = `${path}.tmp`;
  await writeFile(tmp, JSON.stringify(state), "utf-8");
  await rename(tmp, path);
}

// ---------------------------------------------------------------------------
// DB discovery
// ---------------------------------------------------------------------------

type SqliteModule = typeof import("node:sqlite");
type SqliteDb = InstanceType<SqliteModule["DatabaseSync"]>;

async function loadSqlite(): Promise<SqliteModule | null> {
  try {
    return await import("node:sqlite");
  } catch {
    return null; // Node < 22.5 — degrade to message-capture-only.
  }
}

function openReadOnly(sqlite: SqliteModule, path: string): SqliteDb | null {
  try {
    return new sqlite.DatabaseSync(path, { readOnly: true });
  } catch {
    return null;
  }
}

function hasTaskRunsTable(db: SqliteDb): boolean {
  try {
    const row = db
      .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'")
      .get();
    return !!row;
  } catch {
    return false;
  }
}

/**
 * Locate EVERY SQLite file holding ``task_runs`` — not a single winner.
 *
 * First-match discovery cannot distinguish a valid-but-DEAD DB from the
 * live one: an OpenClaw 2026.5 -> 2026.6 upgrade can leave the old
 * ``tasks/runs.sqlite`` on disk with a valid (but frozen) task_runs
 * table while all new rows land in the consolidated state DB — any
 * single-winner precedence keeps returning the stale file and the sync
 * reports synced:0 forever, indistinguishable from "node idle". So the
 * sync reads ALL candidates: the sidecar's task_id-keyed delta makes
 * multi-source safe (rows migrated into the new DB de-dup as already
 * seen), and the shallow scan is a trivial hourly readdir.
 *
 * ``CAURA_TASK_DB_PATH`` remains an exclusive operator override.
 */
function discoverTaskDbs(sqlite: SqliteModule): string[] {
  const tryPath = (p: string): boolean => {
    if (!existsSync(p)) return false;
    const db = openReadOnly(sqlite, p);
    if (!db) return false;
    try {
      return hasTaskRunsTable(db);
    } finally {
      // Same pattern as the read loop: a throwing close() must neither
      // leak the handle nor surface as a bogus task-db-unavailable.
      try {
        db.close();
      } catch {
        // best-effort close; the handle is read-only
      }
    }
  };

  const overridePath = taskDbPathOverride();
  if (overridePath) {
    return tryPath(overridePath) ? [overridePath] : [];
  }

  // Legacy path first for deterministic ordering; the scan re-finds it,
  // so results are deduped by path string (identical join construction).
  const candidates: string[] = [join(baseDir(), "tasks", "runs.sqlite")];
  const scanDir = (dir: string, depth: number): void => {
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      return;
    }
    for (const name of entries) {
      if (name.endsWith("-wal") || name.endsWith("-shm") || name.startsWith(".")) continue;
      const full = join(dir, name);
      let st;
      try {
        st = statSync(full);
      } catch {
        continue; // TOCTOU: vanished between readdir and stat
      }
      if (st.isFile() && name.endsWith(".sqlite")) candidates.push(full);
      else if (st.isDirectory() && depth > 0 && name !== "plugins") scanDir(full, depth - 1);
    }
  };
  scanDir(baseDir(), 1);
  return [...new Set(candidates)].filter(tryPath);
}

// ---------------------------------------------------------------------------
// Column probe + row reading
// ---------------------------------------------------------------------------

function probeColumns(db: SqliteDb): Set<string> | null {
  try {
    const rows = db.prepare("PRAGMA table_info(task_runs)").all() as Array<{ name: string }>;
    const cols = new Set(rows.map((r) => r.name));
    for (const required of REQUIRED_COLUMNS) {
      if (!cols.has(required)) return null;
    }
    return cols;
  } catch {
    return null;
  }
}

function readRecentRows(db: SqliteDb, cols: Set<string>, cutoffMs: number): TaskRow[] {
  const available = [
    ...REQUIRED_COLUMNS,
    ...OPTIONAL_COLUMNS.filter((c) => cols.has(c)),
  ];
  // Window on last_event_at when present (indexed in every schema we've
  // seen), else created_at. Retention already bounds the table to ~7d.
  const timeCol = cols.has("last_event_at") ? "last_event_at" : "created_at";
  const sql =
    `SELECT ${available.join(", ")} FROM task_runs ` +
    `WHERE COALESCE(${timeCol}, created_at) >= ? ` +
    `ORDER BY COALESCE(${timeCol}, created_at) ASC, task_id ASC`;
  const raw = db.prepare(sql).all(cutoffMs) as Array<Record<string, unknown>>;
  return raw.map((r) => ({
    task_id: String(r.task_id),
    status: String(r.status ?? ""),
    created_at: Number(r.created_at ?? 0),
    last_event_at: r.last_event_at == null ? null : Number(r.last_event_at),
    ended_at: r.ended_at == null ? null : Number(r.ended_at),
    task: String(r.task ?? ""),
    label: r.label == null ? null : String(r.label),
    runtime: r.runtime == null ? null : String(r.runtime),
    agent_id: r.agent_id == null ? null : String(r.agent_id),
    progress_summary: r.progress_summary == null ? null : String(r.progress_summary),
    terminal_summary: r.terminal_summary == null ? null : String(r.terminal_summary),
    terminal_outcome: r.terminal_outcome == null ? null : String(r.terminal_outcome),
    error: r.error == null ? null : String(r.error),
    child_session_key: r.child_session_key == null ? null : String(r.child_session_key),
    requester_session_key:
      r.requester_session_key == null ? null : String(r.requester_session_key),
  }));
}

// ---------------------------------------------------------------------------
// Row -> C2 event mapping
// ---------------------------------------------------------------------------

/**
 * A row is terminal when the runtime stamped ``ended_at``. Deliberately
 * NOT an enum check on ``status`` — status vocabularies drift across
 * OpenClaw versions, ``ended_at`` semantics don't.
 */
function isTerminal(row: TaskRow): boolean {
  return row.ended_at != null;
}

function taskHeader(row: TaskRow): string {
  const scope = [row.runtime, row.label].filter(Boolean).join("/");
  return scope ? `[task ${scope}] ` : "[task] ";
}

function discoveredEvent(row: TaskRow) {
  return {
    session_id: row.child_session_key ?? row.requester_session_key ?? null,
    role: "agent",
    kind: "task",
    content: taskHeader(row) + row.task,
    ...(row.runtime ? { tool: row.runtime } : {}),
  };
}

function terminalEvent(row: TaskRow) {
  const summary =
    row.terminal_summary ?? row.progress_summary ?? row.error ?? `task ended: ${row.status}`;
  return {
    session_id: row.child_session_key ?? row.requester_session_key ?? null,
    role: "agent",
    kind: "task_result",
    content: taskHeader(row) + summary,
    ...(row.runtime ? { tool: row.runtime } : {}),
    outcome: row.terminal_outcome ?? row.status,
  };
}

// ---------------------------------------------------------------------------
// The sync
// ---------------------------------------------------------------------------

/**
 * Mirror the task-trail delta into the interview buffer. Emits at most
 * TWO events per task — one when it first appears, one when it reaches
 * a terminal state — so long-running tasks don't spam the buffer on
 * every progress bump. Bounded by ``INTERVIEW_TASK_SYNC_MAX_PER_TICK``
 * per call; the sidecar is only advanced for emitted rows, so the
 * remainder drains naturally on later ticks.
 *
 * Never throws.
 */
export async function syncTaskTrail(): Promise<TaskSyncResult> {
  // Hoisted past the outer catch: events appended to the buffer BEFORE a
  // mid-loop failure are real (the buffer is append-only and the sidecar
  // hasn't advanced), so a partial sync must report them — not synced:0.
  let appended = 0;
  // Flipped once the task DB(s) have been read successfully: from that
  // point on, any failure is in the buffer/sidecar layer, not the DB.
  let dbReadStarted = false;
  try {
    const sqlite = await loadSqlite();
    if (!sqlite) {
      return { synced: 0, note: "task-db-unavailable: node:sqlite not present in this runtime" };
    }
    const sidecar = loadSidecar();
    const dbPaths = discoverTaskDbs(sqlite);
    if (dbPaths.length === 0) {
      // A configured-but-broken override deserves a pointed diagnostic —
      // the generic "not found" reads as an idle/undiscovered node and
      // sends the operator hunting in the wrong place.
      const overridePath = taskDbPathOverride();
      const note = overridePath
        ? `task-db-unavailable: CAURA_TASK_DB_PATH=${overridePath} has no task_runs table`
        : "task-db-unavailable: no task_runs database found";
      return { synced: 0, note };
    }
    const cutoff = Date.now() - INTERVIEW_TASK_SIDECAR_RETENTION_MS;
    // Rows from EVERY valid DB, merged: after a 2026.5 -> 2026.6 upgrade
    // both the frozen legacy file and the live consolidated DB may hold a
    // task_runs table — reading all of them makes "which one is live" a
    // non-question. Same task_id in two DBs (installer-migrated rows)
    // de-dups through the sidecar like any already-seen task. A DB that
    // fails to open/probe here is skipped, not fatal: the others still sync.
    const rows: TaskRow[] = [];
    const unreadable: string[] = [];
    for (const dbPath of dbPaths) {
      const db = openReadOnly(sqlite, dbPath);
      if (!db) {
        unreadable.push(dbPath);
        continue;
      }
      try {
        const cols = probeColumns(db);
        if (!cols) {
          unreadable.push(dbPath);
          continue;
        }
        // Plain loop, not rows.push(...arr): spread maps elements to call
        // ARGUMENTS, and V8 caps those (~65k) — a busy node's 8-day window
        // could exceed it and RangeError into the outer catch, masquerading
        // as task-db-unavailable and dropping the whole sync.
        const newRows = readRecentRows(db, cols, cutoff);
        for (const r of newRows) rows.push(r);
      } finally {
        try {
          db.close();
        } catch {
          // best-effort close; the handle is read-only
        }
      }
    }
    if (rows.length === 0 && unreadable.length === dbPaths.length) {
      return {
        synced: 0,
        note: `task-db-unavailable: no readable task_runs schema among ${dbPaths.join(", ")}`,
      };
    }
    // Each DB's rows arrive internally ordered, but the multi-DB merge is
    // in DB-discovery order — re-sort globally so buffer narrative and,
    // more importantly, cap DEFERRAL are chronological: without this, the
    // cap would defer whichever DB happened to be scanned last, not the
    // newest work.
    rows.sort((a, b) => {
      const ta = a.last_event_at ?? a.created_at;
      const tb = b.last_event_at ?? b.created_at;
      return ta !== tb ? ta - tb : a.task_id < b.task_id ? -1 : 1;
    });

    {
      dbReadStarted = true;
      let capped = false;
      for (const row of rows) {
        if (appended >= INTERVIEW_TASK_SYNC_MAX_PER_TICK) {
          capped = true;
          break;
        }
        const known = sidecar.tasks[row.task_id];
        const eventTs = row.last_event_at ?? row.created_at;
        if (!known) {
          await appendInterviewEvent(discoveredEvent(row));
          appended += 1;
          // ``terminal_emitted`` must reflect what was ACTUALLY appended:
          // when the cap fires right after the discovered event, the
          // terminal half is deferred — recording it as emitted here would
          // suppress it forever (the next tick's terminal-transition check
          // would see terminal_emitted:true and skip).
          const nowTerminal = isTerminal(row);
          const terminalEmitted = nowTerminal && appended < INTERVIEW_TASK_SYNC_MAX_PER_TICK;
          if (terminalEmitted) {
            // Task appeared AND finished between ticks: emit both halves.
            await appendInterviewEvent(terminalEvent(row));
            appended += 1;
          } else if (nowTerminal) {
            // Terminal half deferred by the cap: surface it in the note —
            // the row-level guard at the top of the loop never fires when
            // the LAST row's second half is what got deferred.
            capped = true;
          }
          sidecar.tasks[row.task_id] = {
            status: row.status,
            last_event_at: eventTs,
            terminal_emitted: terminalEmitted,
          };
        } else if (isTerminal(row) && !known.terminal_emitted) {
          await appendInterviewEvent(terminalEvent(row));
          appended += 1;
          known.status = row.status;
          known.last_event_at = eventTs;
          known.terminal_emitted = true;
        } else {
          // Progress-only change: track freshness, emit nothing.
          known.status = row.status;
          known.last_event_at = Math.max(known.last_event_at, eventTs);
        }
      }

      // Prune sidecar entries older than the retention horizon — the DB
      // itself prunes at ~7d, so anything older can never resurface.
      for (const [id, st] of Object.entries(sidecar.tasks)) {
        if (st.last_event_at < cutoff) delete sidecar.tasks[id];
      }
      sidecar.db_paths = dbPaths;
      // Sidecar advances AFTER the appends: a crash between the two
      // re-emits (duplicate narrative, harmless) rather than drops.
      await saveSidecar(sidecar);

      const notes: string[] = [];
      if (capped) notes.push(`capped at ${INTERVIEW_TASK_SYNC_MAX_PER_TICK} events; remainder next tick`);
      if (unreadable.length > 0) notes.push(`skipped unreadable: ${unreadable.join(", ")}`);
      return notes.length > 0 ? { synced: appended, note: notes.join("; ") } : { synced: appended };
    }
  } catch (e: unknown) {
    // Distinguish the failure domain for operators (events already written
    // are always reported — the sidecar didn't advance, so the next tick
    // re-emits the tail: duplicates, never loss):
    //   appended > 0   -> appends succeeded, a later step failed;
    //   dbReadStarted  -> DB was readable, the buffer/sidecar layer failed
    //                     before the first append landed;
    //   otherwise      -> never got to reading rows: genuinely a DB problem.
    return {
      synced: appended,
      note:
        appended > 0
          ? `partial-sync-error: ${String(e)}`
          : dbReadStarted
            ? `task-trail-error: ${String(e)}`
            : `task-db-unavailable: ${String(e)}`,
    };
  }
}
