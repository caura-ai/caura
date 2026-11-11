/** Private write-ahead intent and bounded retries for node-reported results. */
import {
  constants, openSync, closeSync, fstatSync, readSync,
  writeFileSync, fsyncSync, renameSync, rmSync, mkdirSync, existsSync, lstatSync,
} from "node:fs";
import { dirname, basename, isAbsolute, join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { getPluginDir } from "./paths.js";
import { CAURA_TENANT_ID } from "./env.js";
import { apiCall } from "./transport.js";
import { logError } from "./logger.js";

const MAX_BYTES = 1024 * 1024;
const MAX_EVENTS = 256;
const MAX_REJECTED = 256;
const BATCH = 64;
const MAX_REQUESTS = 8;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH = /^sha256:[0-9a-f]{64}$/;
const SKILL = /^(?:(?:forge|agent)\/)?[a-z0-9][a-z0-9._-]{0,99}$/;

export interface SkillArtifact {
  skill_doc_id: string;
  grant_id: string;
  artifact_hash: string;
}
interface InstallationEvent extends SkillArtifact {
  event_id: string;
  target_id: string;
  occurred_at: string;
  outcome: "installed" | "updated" | "removed";
}
interface Installed extends SkillArtifact { file: string; target_id: string }
interface Pending { file: string; state: "intent" | "ready"; event: InstallationEvent; ownership_marker?: string }
interface Rejected {
  file: string;
  event: InstallationEvent;
  tenant_id: string;
  node_id: string;
  status: 401 | 403 | 404 | 409 | 422;
  rejected_at: string;
  reason?: "event_rejected" | "superseded_receiver";
  replacement_node_id?: string;
}
interface JournalState {
  version: 1;
  tenant_id: string;
  node_id: string;
  targets: Array<{ dir: string; target_id: string }>;
  installed: Installed[];
  events: Pending[];
  rejected: Rejected[];
}

export function isSkillGrantId(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function artifact(value: SkillArtifact, legacy = false): boolean {
  return typeof value?.skill_doc_id === "string" && SKILL.test(value.skill_doc_id)
    // Read older non-UUID IDs so their pending reports can receive an
    // explicit rejection decision. New mutations require a canonical UUID.
    && (legacy ? typeof value.grant_id === "string" && value.grant_id.length > 0 && value.grant_id.length <= 128
      : isSkillGrantId(value.grant_id))
    && typeof value.artifact_hash === "string" && HASH.test(value.artifact_hash);
}
function skillFile(file: unknown): file is string {
  return typeof file === "string" && isAbsolute(file) && basename(file) === "SKILL.md"
    && /^[a-z0-9][a-z0-9._-]{0,99}$/.test(basename(dirname(file)));
}
function openRegular(file: string): number {
  const before = lstatSync(file);
  if (!before.isFile() || before.isSymbolicLink()) throw new Error("Installation state must be a regular file");
  const fd = openSync(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  const opened = fstatSync(fd);
  if (!opened.isFile() || opened.ino !== before.ino || opened.dev !== before.dev) {
    closeSync(fd);
    throw new Error("Installation file changed while opening it");
  }
  return fd;
}

/** POSIX directory durability; Windows does not expose directory fsync here. */
export function syncSkillDirectory(dir: string): void {
  if (process.platform === "win32") return;
  const fd = openSync(dir, "r");
  try { fsyncSync(fd); }
  finally { closeSync(fd); }
}

function load(file: string, nodeId: string): JournalState {
  let fd: number;
  try { fd = openRegular(file); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { version: 1, tenant_id: CAURA_TENANT_ID, node_id: nodeId, targets: [], installed: [], events: [], rejected: [] };
    }
    throw error;
  }
  let state: JournalState;
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > MAX_BYTES || (process.platform !== "win32" && (stat.mode & 0o077) !== 0)) {
      throw new Error("Installation state must be private, regular and at most 1 MiB");
    }
    const bytes = Buffer.alloc(MAX_BYTES + 1);
    const length = readSync(fd, bytes, 0, bytes.length, 0);
    if (length > MAX_BYTES) throw new Error("Installation state exceeds 1 MiB");
    state = JSON.parse(new TextDecoder("utf8", { fatal: true }).decode(bytes.subarray(0, length)));
  } finally { closeSync(fd); }
  // Validate the entire state before considering an idle node rebind. An
  // incompatible version, another tenant or corrupt data is never reset.
  if (state?.version !== 1 || state.tenant_id !== CAURA_TENANT_ID || !UUID.test(state.node_id)
      || !Array.isArray(state.targets) || !Array.isArray(state.installed) || !Array.isArray(state.events)
      || state.events.length > MAX_EVENTS) throw new Error("Incompatible installation state or receiver binding");
  if (state.rejected === undefined) state.rejected = [];
  if (!Array.isArray(state.rejected) || state.rejected.length > MAX_REJECTED) throw new Error("Invalid rejected installation state");
  for (const target of state.targets) {
    if (!target || typeof target.dir !== "string" || !isAbsolute(target.dir) || !UUID.test(target.target_id)) {
      throw new Error("Invalid installation target state");
    }
  }
  for (const installed of state.installed) {
    if (!installed || !skillFile(installed.file) || !UUID.test(installed.target_id) || !artifact(installed, true)) {
      throw new Error("Invalid installed artifact state");
    }
  }
  const ids = new Set<string>();
  for (const pending of state.events) {
    const event = pending?.event;
    if (!pending || !skillFile(pending.file) || !["intent", "ready"].includes(pending.state)
        || !artifact(event, true) || !UUID.test(event.event_id) || !UUID.test(event.target_id)
        || !["installed", "updated", "removed"].includes(event.outcome)
        || typeof event.occurred_at !== "string" || !Number.isFinite(Date.parse(event.occurred_at))
        || ids.has(event.event_id)) throw new Error("Invalid pending installation state");
    if (pending.ownership_marker !== undefined
        && (typeof pending.ownership_marker !== "string" || basename(pending.ownership_marker) !== pending.ownership_marker)) {
      throw new Error("Invalid installation ownership marker");
    }
    ids.add(event.event_id);
  }
  for (const rejected of state.rejected) {
    const event = rejected?.event;
    const superseded = rejected?.reason === "superseded_receiver";
    const decision = !!rejected && (superseded
      ? [401, 403, 404].includes(rejected.status) && UUID.test(rejected.replacement_node_id ?? "")
        && rejected.replacement_node_id !== rejected.node_id
      : (rejected.reason === undefined || rejected.reason === "event_rejected") && [409, 422].includes(rejected.status));
    if (!rejected || !decision || !skillFile(rejected.file) || !artifact(event, true)
        || !UUID.test(event.event_id) || !UUID.test(event.target_id)
        || !["installed", "updated", "removed"].includes(event.outcome)
        || typeof event.occurred_at !== "string" || !Number.isFinite(Date.parse(event.occurred_at))
        || typeof rejected.tenant_id !== "string" || !rejected.tenant_id
        || !UUID.test(rejected.node_id)
        || typeof rejected.rejected_at !== "string" || !Number.isFinite(Date.parse(rejected.rejected_at))
        || ids.has(event.event_id)) throw new Error("Invalid rejected installation record");
    ids.add(event.event_id);
  }
  return state;
}

function fileHash(file: string): string | undefined {
  // Hash only a bounded regular skill file; never follow a substituted link.
  let fd: number;
  try { fd = openRegular(file); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > 65_536) throw new Error("Cannot verify a nonregular or oversized skill file");
    const bytes = Buffer.alloc(65_537);
    const length = readSync(fd, bytes, 0, bytes.length, 0);
    if (length > 65_536) throw new Error("Skill file grew beyond 64 KiB");
    return "sha256:" + createHash("sha256").update(bytes.subarray(0, length)).digest("hex");
  } finally { closeSync(fd); }
}
function absent(file: string): boolean {
  try { lstatSync(file); return false; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return true;
    throw error;
  }
}

export class SkillInstallations {
  private readonly file = join(getPluginDir(), "skill-installations.json");
  private readonly state: JournalState;
  private blocked = false;
  private readonly attempted = new Set<string>();
  private requests = 0;

  constructor(private readonly receiverNodeId: string) {
    this.state = load(this.file, receiverNodeId);
    // Resolve old intent under its original identity before considering a
    // rebind. Ready results can then drain even after heartbeat replaces it.
    this.recover();
    this.rebindIfDrained();
  }

  private rebindIfDrained(): void {
    if (!this.blocked && this.state.node_id !== this.receiverNodeId && !this.state.events.length) {
      // No active results remain. Old approvals are not the replacement's
      // installation history. Quarantine retains its own original origin.
      this.state.node_id = this.receiverNodeId;
      this.state.installed = [];
      this.state.targets = [];
      this.save();
    }
  }

  canMutate(): boolean {
    return !this.blocked && this.state.node_id === this.receiverNodeId;
  }

  private save(): void {
    let temporary: string | undefined;
    try {
      const bytes = Buffer.from(JSON.stringify(this.state), "utf8");
      if (bytes.length > MAX_BYTES || this.state.events.length > MAX_EVENTS
          || this.state.rejected.length > MAX_REJECTED) throw new Error("Installation outbox capacity reached");
      mkdirSync(dirname(this.file), { recursive: true, mode: 0o700 });
      temporary = `${this.file}.${randomUUID()}.tmp`;
      const fd = openSync(temporary, "wx", 0o600);
      try { writeFileSync(fd, bytes); fsyncSync(fd); }
      finally { closeSync(fd); }
      renameSync(temporary, this.file);
      syncSkillDirectory(dirname(this.file));
    } catch (error) {
      // Do not send in-memory completions if their durable update failed.
      // The previous on-disk intent remains recoverable on the next tick.
      this.blocked = true;
      throw error;
    } finally {
      if (temporary) rmSync(temporary, { force: true });
    }
  }

  private complete(pending: Pending): void {
    this.state.installed = this.state.installed.filter((entry) => entry.file !== pending.file);
    if (pending.event.outcome !== "removed") {
      const { skill_doc_id, grant_id, artifact_hash, target_id } = pending.event;
      this.state.installed.push({ file: pending.file, skill_doc_id, grant_id, artifact_hash, target_id });
    }
    pending.state = "ready";
  }

  private recover(): void {
    const intents = this.state.events.filter((pending) => pending.state === "intent");
    if (!intents.length) return;
    for (const pending of intents) {
      const performed = pending.event.outcome === "removed"
        ? absent(dirname(pending.file))
        : (!pending.ownership_marker || existsSync(join(dirname(pending.file), pending.ownership_marker)))
          && fileHash(pending.file) === pending.event.artifact_hash;
      if (performed) this.complete(pending);
      else this.state.events = this.state.events.filter((entry) => entry !== pending);
    }
    this.save();
  }

  private capacity(): void {
    if (!this.canMutate() || this.state.events.length >= MAX_EVENTS) {
      throw new Error("Skill changes paused: installation results cannot be preserved");
    }
  }

  private reserve(file: string, value: SkillArtifact, outcome: InstallationEvent["outcome"], marker?: string): Pending {
    if (!artifact(value)) throw new Error("Invalid installation artifact binding");
    this.capacity();
    const dir = dirname(dirname(file));
    let target = this.state.targets.find((entry) => entry.dir === dir);
    if (!target) {
      target = { dir, target_id: randomUUID() };
      this.state.targets.push(target);
    }
    const pending: Pending = {
      file, state: "intent",
      ...(marker ? { ownership_marker: marker } : {}),
      event: { ...value, outcome, target_id: target.target_id, event_id: randomUUID(), occurred_at: new Date().toISOString() },
    };
    this.state.events.push(pending);
    // Intent, including stable ID and exact artifact, must reach disk BEFORE
    // the callback is allowed to change SKILL.md or remove its directory.
    this.save();
    return pending;
  }

  write(file: string, value: SkillArtifact, operation: () => void | false, marker?: string): boolean {
    const pending = this.reserve(file, value, existsSync(file) ? "updated" : "installed", marker);
    if (operation() === false) {
      // A foreign directory won the additive create race. No write was
      // attempted, so retire intent without reporting a successful result.
      this.state.events = this.state.events.filter((entry) => entry !== pending);
      this.save();
      return false;
    }
    if (fileHash(file) !== value.artifact_hash) throw new Error("Installed skill bytes did not verify");
    syncSkillDirectory(dirname(dirname(file)));
    this.complete(pending);
    this.save();
    return true;
  }

  remove(file: string, operation: () => void): void {
    this.capacity();
    const installed = this.state.installed.find((entry) => entry.file === file);
    if (!installed) {
      // Preserve existing legacy pruning semantics without inventing an
      // approval/hash history for files this journal never materialized.
      operation();
      return;
    }
    if (fileHash(file) !== installed.artifact_hash) throw new Error("Changed local skill needs operator review before removal reporting");
    const { skill_doc_id, grant_id, artifact_hash } = installed;
    const pending = this.reserve(file, { skill_doc_id, grant_id, artifact_hash }, "removed");
    operation();
    if (!absent(dirname(file))) throw new Error("Skill directory removal did not complete");
    syncSkillDirectory(dirname(dirname(file)));
    this.complete(pending);
    this.save();
  }

  private quarantine(pending: Pending, status: Rejected["status"], superseded = false): void {
    if (this.state.rejected.length >= MAX_REJECTED) {
      logError("Skill installation rejection remains pending", `event_id=${pending.event.event_id}: rejected archive full`);
      return;
    }
    this.state.rejected.push({
      file: pending.file, event: pending.event, tenant_id: this.state.tenant_id,
      node_id: this.state.node_id, status, rejected_at: new Date().toISOString(),
      reason: superseded ? "superseded_receiver" : "event_rejected",
      ...(superseded ? { replacement_node_id: this.receiverNodeId } : {}),
    });
    this.state.events = this.state.events.filter((entry) => entry !== pending);
    this.save();
    logError("Skill installation report quarantined", `event_id=${pending.event.event_id} node_id=${this.state.node_id} status=${status}`
      + (superseded ? ` reason=superseded_receiver replacement_node_id=${this.receiverNodeId}` : ""));
  }

  private async submit(batch: Pending[]): Promise<void> {
    if (this.blocked || this.requests >= MAX_REQUESTS) return;
    this.requests++;
    const ids = batch.map((entry) => entry.event.event_id);
    for (const id of ids) this.attempted.add(id);
    try {
      // Include isolation requests in the same per-tick budget. A poison
      // report must not hold heartbeat behind 64 individual timeouts.
      const result = await apiCall("POST", "/skills/installations", {
        tenant_id: this.state.tenant_id, node_id: this.state.node_id,
        reporting_version: 1, events: batch.map((entry) => entry.event),
      }, undefined, AbortSignal.timeout(2000)) as Record<string, unknown>;
      const accepted = result?.accepted_event_ids;
      if (result?.reporting_version !== 1 || result.node_id !== this.state.node_id
          || !Array.isArray(accepted) || accepted.length !== ids.length
          || new Set(accepted).size !== ids.length || !ids.every((id) => accepted.includes(id))) {
        throw new Error("Installation results were not acknowledged in full");
      }
      const acceptedIds = new Set(ids);
      this.state.events = this.state.events.filter((entry) => !acceptedIds.has(entry.event.event_id));
      this.save();
    } catch (error) {
      if (this.blocked) return;
      const retired = this.state.node_id !== this.receiverNodeId && error instanceof Error
        ? /^Caura API (401|403|404):/.exec(error.message) : null;
      if (retired) {
        // Heartbeat already supplied a different receiver. After actually
        // attempting the old binding, retain its refused reports locally as
        // superseded, not accepted or claimed to be permanently invalid.
        // Current-receiver auth/route errors still take the retryable path.
        for (const pending of batch) this.quarantine(pending, Number(retired[1]) as 401 | 403 | 404, true);
        return;
      }
      const match = error instanceof Error ? /^Caura API (409|422):/.exec(error.message) : null;
      if (match) {
        if (batch.length === 1) this.quarantine(batch[0], Number(match[1]) as 409 | 422);
        else for (const pending of batch) {
          if (this.blocked || this.requests >= MAX_REQUESTS) break;
          await this.submit([pending]);
        }
      } else {
        // Authentication/permission, missing route, rate limits, timeout,
        // network and server failures can recover; preserve the same IDs.
        logError("Skill installation results remain pending", error);
      }
    }
  }

  async flush(): Promise<void> {
    if (this.blocked) return;
    const ready = this.state.events.filter((entry) => entry.state === "ready" && !this.attempted.has(entry.event.event_id));
    try {
      for (let start = 0; start < ready.length; start += BATCH) {
        if (this.blocked || this.requests >= MAX_REQUESTS) break;
        await this.submit(ready.slice(start, start + BATCH));
      }
    } finally {
      this.rebindIfDrained();
    }
  }
}
