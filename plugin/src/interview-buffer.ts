/**
 * Interviewer Phase 1 — the node-local on-disk event buffer.
 *
 * An append-only JSONL trail of conversation events, keyed by a per-node
 * monotonic ``seq``. The Interviewer's crash-safety hangs on this file:
 * the in-memory session buffer (context-engine) is an LRU that drops old
 * events even without a crash, so the durable window an
 * ``interview_request`` reads MUST live on disk
 * (docs/plans/interviewer-phase1-decisions.md, Fork 1 — in-process
 * buffering is rejected permanently).
 *
 * Contract with the server (frozen in PR #558):
 * - events are C2-shaped: {seq, ts, session_id, role, kind, content,
 *   tool?, outcome?}; content ≤ 8000 chars, tool/outcome ≤ 200 (the
 *   submit endpoint 422s on oversize rather than silently truncating);
 * - ``seq`` is strictly monotonic per node and survives gateway
 *   restarts (re-seeded from the file tail on load);
 * - the buffer is pruned ONLY through the committed watermark returned
 *   by ``POST /interview/submit`` — never optimistically.
 *
 * Writes are serialized through a promise chain so concurrent ingest
 * calls can't interleave partial lines; reads/prunes join the same chain
 * so they always observe a consistent file.
 */

import { appendFile, mkdir, readFile, rename, stat, writeFile } from "fs/promises";
import { dirname, join } from "path";

import { getPluginDir } from "./paths.js";
import {
  INTERVIEW_BUFFER_MAX_BYTES,
  INTERVIEW_EVENT_MAX_CHARS,
  INTERVIEW_FIELD_MAX_CHARS,
} from "./env.js";

export interface InterviewEvent {
  seq: number;
  ts: string;
  session_id: string | null;
  role: string;
  kind: string;
  content: string;
  tool?: string;
  outcome?: string;
}

export interface InterviewEventInput {
  session_id?: string | null;
  role: string;
  kind: string;
  content: string;
  tool?: string;
  outcome?: string;
}

/** ~/.openclaw/plugins/memclaw/interview-buffer.jsonl */
export function getInterviewBufferPath(): string {
  return _pathOverride ?? join(getPluginDir(), "interview-buffer.jsonl");
}

/**
 * Sidecar carrying the next seq across the one state the JSONL tail can't
 * represent: an EMPTY buffer. After a full prune + gateway restart,
 * seeding from the (empty) tail would restart seq at 0 — BELOW the
 * server's watermark, making every new event invisible to the next
 * interview window forever. Found by the VM wet test (task #6); the meta
 * file is written on prune/compact so a restart seeds from
 * max(tail+1, meta.next_seq).
 */
function getInterviewMetaPath(): string {
  return getInterviewBufferPath() + ".meta.json";
}

let _pathOverride: string | undefined;
let _nextSeq: number | undefined; // lazily seeded from the file tail
let _approxBytes = 0;
// Serialization point for every mutation (append / prune / compact).
let _chain: Promise<void> = Promise.resolve();

function _enqueue<T>(fn: () => Promise<T>): Promise<T> {
  const run = _chain.then(fn);
  // The chain must survive individual failures — swallow here, callers
  // still see the rejection through ``run``.
  _chain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

function _parseLines(raw: string): InterviewEvent[] {
  const events: InterviewEvent[] = [];
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    try {
      const ev = JSON.parse(line) as InterviewEvent;
      if (typeof ev.seq === "number") events.push(ev);
    } catch {
      // A torn final line from a crash mid-append is expected once; skip.
    }
  }
  return events;
}

async function _load(): Promise<InterviewEvent[]> {
  try {
    const raw = await readFile(getInterviewBufferPath(), "utf-8");
    return _parseLines(raw);
  } catch {
    return []; // ENOENT → empty buffer
  }
}

async function _readMetaNextSeq(): Promise<number> {
  try {
    const raw = await readFile(getInterviewMetaPath(), "utf-8");
    const meta = JSON.parse(raw) as { next_seq?: number };
    return typeof meta.next_seq === "number" && meta.next_seq >= 0 ? meta.next_seq : 0;
  } catch {
    return 0;
  }
}

async function _writeMetaNextSeq(nextSeq: number): Promise<void> {
  const path = getInterviewMetaPath();
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, JSON.stringify({ next_seq: nextSeq }), "utf-8");
}

async function _ensureSeeded(): Promise<void> {
  if (_nextSeq !== undefined) return;
  const path = getInterviewBufferPath();
  // Self-heal a torn final line that lacks its newline (crash mid-append):
  // without this, the NEXT append concatenates onto the torn fragment and
  // the merged line swallows one real event. Found by the VM wet test.
  try {
    const raw = await readFile(path, "utf-8");
    if (raw.length > 0 && !raw.endsWith("\n")) {
      await appendFile(path, "\n", "utf-8");
    }
  } catch (err: unknown) {
    // Only a missing file is benign. A FAILED heal (disk full, EACCES)
    // must propagate: swallowing it would let the next append concatenate
    // onto the torn fragment and lose both events. _nextSeq is still
    // unset here, so seeding is retried on the next enqueued operation.
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
    // ENOENT — file does not exist yet, nothing to heal.
  }
  const events = await _load();
  const tailSeq = events.length ? events[events.length - 1].seq + 1 : 0;
  const metaSeq = await _readMetaNextSeq();
  _nextSeq = Math.max(tailSeq, metaSeq);
  try {
    _approxBytes = (await stat(path)).size;
  } catch {
    _approxBytes = 0;
  }
}

async function _rewrite(events: InterviewEvent[]): Promise<void> {
  const path = getInterviewBufferPath();
  const tmp = `${path}.tmp`;
  const body = events.map((e) => JSON.stringify(e)).join("\n") + (events.length ? "\n" : "");
  await mkdir(dirname(path), { recursive: true });
  await writeFile(tmp, body, "utf-8");
  await rename(tmp, path); // atomic swap — a crash never leaves a torn file
  _approxBytes = Buffer.byteLength(body);
}

/**
 * Append one event. Fire-and-forget safe: errors are the caller's to log,
 * ordering is guaranteed by the internal chain. Returns the assigned seq.
 */
export function appendInterviewEvent(input: InterviewEventInput): Promise<number> {
  return _enqueue(async () => {
    await _ensureSeeded();
    const seq = _nextSeq!;
    _nextSeq = seq + 1;
    const ev: InterviewEvent = {
      seq,
      ts: new Date().toISOString(),
      session_id: input.session_id ?? null,
      role: (input.role || "unknown").slice(0, 64),
      kind: (input.kind || "message").slice(0, 64),
      content: (input.content || "").slice(0, INTERVIEW_EVENT_MAX_CHARS),
    };
    if (input.tool) ev.tool = input.tool.slice(0, INTERVIEW_FIELD_MAX_CHARS);
    if (input.outcome) ev.outcome = input.outcome.slice(0, INTERVIEW_FIELD_MAX_CHARS);
    const line = JSON.stringify(ev) + "\n";
    const path = getInterviewBufferPath();
    await mkdir(dirname(path), { recursive: true });
    await appendFile(path, line, "utf-8");
    _approxBytes += Buffer.byteLength(line);
    // Size guard: a node that is never interviewed (or whose tenant is
    // enabled but scheduler is down) must not grow the file unbounded.
    // Compact to the newest half — the oldest events are exactly the
    // ones an eventual interview can best afford to lose.
    if (_approxBytes > INTERVIEW_BUFFER_MAX_BYTES) {
      const events = await _load();
      const keep = events.slice(Math.floor(events.length / 2));
      console.warn(
        `[memclaw] interview buffer exceeded ${INTERVIEW_BUFFER_MAX_BYTES} bytes; ` +
          `compacted ${events.length} -> ${keep.length} events (oldest dropped)`,
      );
      // Meta BEFORE the rename — same crash-ordering rule as
      // pruneInterviewBuffer (see there for the full rationale).
      await _writeMetaNextSeq(_nextSeq!);
      await _rewrite(keep);
    }
    return seq;
  });
}

/** Read up to ``maxCount`` events with ``seq >= sinceSeq``, in order. */
export function readInterviewEvents(
  sinceSeq: number,
  maxCount: number,
): Promise<InterviewEvent[]> {
  return _enqueue(async () => {
    await _ensureSeeded();
    const events = await _load();
    return events.filter((e) => e.seq >= sinceSeq).slice(0, maxCount);
  });
}

/**
 * Drop every event with ``seq <= committedSeq``. Called ONLY after the
 * server acknowledged the window as committed (its watermark advanced) —
 * pruning earlier would lose the window on a failed submit.
 */
export function pruneInterviewBuffer(committedSeq: number): Promise<void> {
  return _enqueue(async () => {
    await _ensureSeeded();
    const events = await _load();
    const keep = events.filter((e) => e.seq > committedSeq);
    if (keep.length === events.length) return;
    // Meta BEFORE the rename. A SIGKILL between the two writes must not
    // recreate the seq-reset bug: rewrite-first would leave an emptied
    // buffer with a stale/absent meta, so a restart reseeds at 0 — below
    // the server watermark, hiding all new events. Meta-first's crash
    // window is benign: the buffer keeps its (already-committed) events,
    // the handler reports failed, and the next window reads from
    // watermark+1 — the leftovers are skipped and swept by the next
    // successful prune.
    await _writeMetaNextSeq(_nextSeq!);
    await _rewrite(keep);
  });
}

/** Test-only: point the buffer at a temp file and reset module state. */
export const __INTERVIEW_BUFFER_INTERNALS__ = {
  setPathForTests(path: string | undefined): void {
    if (process.env.NODE_ENV !== "test") {
      throw new Error("setPathForTests is test-only");
    }
    _pathOverride = path;
    _nextSeq = undefined;
    _approxBytes = 0;
    _chain = Promise.resolve();
  },
};
