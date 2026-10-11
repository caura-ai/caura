/**
 * Per-install opaque identifier for the OpenClaw plugin.
 *
 * Without an install_id, multiple plugin installs across a fleet all
 * default to ``agent_id="main"`` and collide on the server's
 * ``(tenant_id, agent_id)`` unique key — every "main" agent's memories,
 * tuning profile, audit trail, and trust gates merge into one bucket.
 *
 * This module emits one stable random suffix per install:
 *
 *   1. First call generates a 12-hex-char (48-bit) suffix from
 *      ``crypto.randomUUID()`` and writes it to ``install.json``
 *      (plugin data dir, see ``paths.ts``). 48 bits keeps the
 *      birthday-paradox collision probability at ~0.003% across 10k
 *      installs per tenant — well below the 8-hex/32-bit suffix's
 *      ~1.2% at the same scale, which would have recreated the very
 *      collision problem this module exists to solve.
 *   2. Every subsequent call reads the cached value from disk.
 *   3. The id is sticky for the install's lifetime — operators can wipe
 *      ``install.json`` to reset, but the plugin itself never rotates.
 *
 * The install_id is **not sensitive** — it's an opaque random suffix
 * comparable to a session id. It appears in heartbeats and tool-call
 * payloads so the server can group agents by install for diagnostics.
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "fs";
import { dirname } from "path";
import { randomUUID } from "crypto";

import { getInstallStatePath } from "./paths.js";
import { logError } from "./logger.js";

let _cached: string | null = null;
let _defaultAgentId: string | null = null;

const _SCHEMA_VERSION = 1;

interface InstallState {
  schema_version: number;
  install_id: string;
  created_at: string;
  /** The id the default agent writes and registers under (M-107). */
  default_agent_id?: string;
}

/**
 * Return the plugin's install_id. Generates and persists on first call.
 *
 * Failure modes:
 *   - Plugin dir not writable (rare — managed installs): a warning is
 *     logged and an in-memory id is returned for the lifetime of the
 *     process. The agent_id will collide on restart but the run continues.
 */
export function getInstallId(): string {
  if (_cached) return _cached;

  const path = getInstallStatePath();

  if (existsSync(path)) {
    try {
      const raw = readFileSync(path, "utf-8");
      const parsed = JSON.parse(raw) as Partial<InstallState>;
      if (typeof parsed.install_id === "string" && parsed.install_id) {
        _cached = parsed.install_id;
        return _cached;
      }
      logError(
        `install.json present but malformed (no install_id) — regenerating`,
        new Error("malformed_install_state"),
      );
    } catch (e) {
      logError(`Failed to read install.json — regenerating`, e);
    }
  }

  const fresh = randomUUID().replace(/-/g, "").slice(0, 12);
  const state: InstallState = {
    schema_version: _SCHEMA_VERSION,
    install_id: fresh,
    created_at: new Date().toISOString(),
    default_agent_id: `main-${fresh}`,
  };
  writeInstallState(
    path,
    state,
    `Failed to persist install.json — install_id will reset on next process start`,
  );

  _cached = fresh;
  return fresh;
}

/**
 * Return the id the install's default agent writes and registers under.
 *
 * With no ``agents.list``, OpenClaw runs one agent, ``main``. A new install
 * records ``main-<install_id>`` when it first writes install.json, so
 * installs sharing a tenant do not merge into one ``main``. An install.json
 * without the record was written by an older plugin, whose turns wrote as
 * ``main``; the plugin's recall filters on the agent's id, so that install
 * keeps ``main`` and records it, and the choice never changes (M-107).
 */
export function getDefaultAgentId(): string {
  if (_defaultAgentId) return _defaultAgentId;
  const installId = getInstallId();
  const path = getInstallStatePath();
  let state: Partial<InstallState> | null = null;
  try {
    state = JSON.parse(readFileSync(path, "utf-8")) as Partial<InstallState>;
  } catch {
    // No readable install.json: getInstallId could not persist one, so this
    // install starts afresh on every process start, as a new install.
  }
  if (typeof state?.default_agent_id === "string" && state.default_agent_id) {
    _defaultAgentId = state.default_agent_id;
  } else if (state?.install_id === installId) {
    _defaultAgentId = "main";
    writeInstallState(
      path,
      { ...state, default_agent_id: "main" } as InstallState,
      `Failed to record the default agent in install.json — it is derived again on next start`,
    );
  } else {
    _defaultAgentId = `main-${installId}`;
  }
  return _defaultAgentId;
}

/**
 * Atomic write: ``writeFileSync`` itself isn't atomic (a crash
 * mid-write leaves a truncated file that the malformed-JSON
 * recovery path interprets as "regenerate", silently rotating the
 * install_id and breaking the sticky-id guarantee). Writing to a
 * sibling ``.tmp`` and then ``renameSync`` makes the publish
 * atomic on POSIX (rename(2) is guaranteed atomic on the same
 * filesystem) and atomic-enough on Windows (replace-on-rename).
 * Readers always see either the previous complete file or the new
 * complete file, never a partial one.
 */
function writeInstallState(path: string, state: InstallState, failure: string): void {
  const tmp = `${path}.tmp`;
  try {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
    renameSync(tmp, path);
  } catch (e) {
    // Best-effort cleanup of an orphaned temp file so the next start
    // doesn't see stale ``.tmp`` debris that could mask future
    // failures. Swallowed — the original error is what matters.
    try {
      if (existsSync(tmp)) rmSync(tmp);
    } catch {
      /* ignore */
    }
    logError(failure, e);
  }
}

/**
 * @internal Test-only: reset the in-memory cache so each test starts
 * fresh. Not part of the plugin's public surface — production code
 * MUST NOT call this. The ``@internal`` tag tells API extractors and
 * documentation tooling to exclude the symbol from public docs; the
 * leading underscore + JSDoc warning are the lint-friendly signal
 * that catches drift if anyone tries to import it from a non-test
 * file.
 */
export function _resetInstallIdCacheForTesting(): void {
  _cached = null;
  _defaultAgentId = null;
}
