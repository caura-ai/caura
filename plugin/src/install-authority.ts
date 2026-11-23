/** Operator-only installation authority. No plugin runtime imports this module. */
import {existsSync} from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { hostname } from "node:os";
import { getInstallAuthorityPath } from "./paths.js";
import {privateInstallState} from "./private-install-state.js";
import { isLoopbackHost, isValidSafeId } from "./validation.js";
import { withUserAgent } from "./user-agent.js";

type Mode = "register" | "handoff" | "resume";
type Input = { bootstrap_credential?: string; registration_key?: string; install_credential?: string };
interface Journal {
  schema_version: 1;
  status: "pending" | "credential_saved" | "verified" | "held";
  mode: "register" | "handoff";
  gateway: string;
  api_prefix: "/api/v1";
  hostname: string;
  platform: string;
  arch: string;
  created_at: string;
  expected_tenant: string;
  expected_fleet?: string;
  install_uuid?: string;
  machine_fingerprint?: string;
  install_credential?: string;
  tenant_id?: string;
  fleet_id?: string | null;
  org_id?: string;
  expires_at?: string | null;
}
export class EnrollmentError extends Error {}
function fail(code: string): never { throw new EnrollmentError(`enrollment_${code}`); }
const text = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= 4096;
const secret = (v: unknown): v is string => text(v) && /^[\x21-\x7e]+$/.test(v);
const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const date = (v: unknown): v is string => typeof v === "string" &&
  /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(v) && Number.isFinite(Date.parse(v));
const expired = (j: Journal) => !!j.expires_at && Date.parse(j.expires_at) <= Date.now();

/** These permissions and durable directory writes require a supported POSIX host. */
export function assertEnrollmentPlatform(): void {
  if (!["darwin", "linux"].includes(process.platform) || !["x64", "arm64"].includes(process.arch))
    fail("platform_unsupported");
}

function gateway(value: string): string {
  let u: URL;
  try { u = new URL(value); } catch { return fail("gateway_invalid"); }
  if (u.username || u.password || u.search || u.hash || u.pathname !== "/" ||
      !(u.protocol === "https:" || (u.protocol === "http:" && isLoopbackHost(u.hostname))))
    fail("gateway_invalid");
  return u.origin;
}

const state = privateInstallState(getInstallAuthorityPath, fail, e => e instanceof EnrollmentError);
const {save, withLock} = state;
function readJournal(): Journal | null {
  const raw = state.read();
  if (raw === undefined) return null;
  const j = raw as Journal;
  if (!j || j.schema_version !== 1 || !["pending", "credential_saved", "verified", "held"].includes(j.status) ||
      !["register", "handoff"].includes(j.mode) || !text(j.gateway) || gateway(j.gateway) !== j.gateway ||
      j.api_prefix !== "/api/v1" || !text(j.hostname) || !text(j.platform) || !text(j.arch) ||
      !date(j.created_at) || !isValidSafeId(j.expected_tenant) ||
      (j.expected_fleet !== undefined && !isValidSafeId(j.expected_fleet)) ||
      (j.install_uuid !== undefined && !uuid(j.install_uuid)) ||
      (j.mode === "register" && (!uuid(j.install_uuid) || !text(j.machine_fingerprint))) ||
      (j.install_credential !== undefined && !secret(j.install_credential)) ||
      (["credential_saved", "verified"].includes(j.status) && !secret(j.install_credential)) ||
      (j.expires_at != null && !date(j.expires_at)) ||
      (j.fleet_id != null && !isValidSafeId(j.fleet_id)) ||
      (j.status === "verified" && (!uuid(j.install_uuid) || !isValidSafeId(j.org_id) ||
        !isValidSafeId(j.tenant_id) || j.tenant_id !== j.expected_tenant || j.fleet_id === undefined ||
        (j.expected_fleet !== undefined && j.expected_fleet !== j.fleet_id))))
    fail("state_invalid");
  return j;
}

export async function requestInstallJson(j: Pick<Journal, "gateway" | "api_prefix">, path: string, key: string | undefined, body?: object): Promise<Record<string, unknown>> {
  try {
    const res = await fetch(`${j.gateway}${j.api_prefix}${path}`, {
      method: body ? "POST" : "GET", redirect: "error", signal: AbortSignal.timeout(10_000),
      headers: withUserAgent({...body ? {"Content-Type": "application/json"} : {}, ...key ? {"X-API-Key": key} : {}}),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) { await res.body?.cancel(); fail(`http_${res.status}`); }
    const reader = res.body?.getReader();
    if (!reader) fail("response_invalid");
    const chunks: Uint8Array[] = []; let bytes = 0;
    try {
      while (true) {
        const {done, value} = await reader.read(); if (done) break;
        bytes += value.byteLength;
        if (bytes > 65536) fail("response_invalid");
        chunks.push(value);
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    let result: unknown;
    try { result = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(Buffer.concat(chunks))); }
    catch { return fail("response_invalid"); }
    if (!result || typeof result !== "object" || Array.isArray(result)) fail("response_invalid");
    return result as Record<string, unknown>;
  } catch (e) {
    if (e instanceof EnrollmentError) throw e;
    return fail("request_failed");
  }
}

function hold(j: Journal, code: string): never { j.status = "held"; save(j); return fail(code); }

/** Read-only; unverified, held or expired authority never becomes usable. */
export function readVerifiedInstallAuthority(): Readonly<Journal> | null {
  assertEnrollmentPlatform();
  if (existsSync(`${getInstallAuthorityPath()}.lock`)) return null;
  const j = readJournal();
  return j?.status === "verified" && !expired(j) && j.hostname === hostname() ? j : null;
}

export async function enrollInstallation(options: {
  mode: Mode; gateway: string; expectedTenant: string; expectedFleet?: string; input: Input;
}): Promise<void> {
  assertEnrollmentPlatform();
  const {mode, input, expectedTenant, expectedFleet} = options;
  const origin = gateway(options.gateway);
  const keys = Object.keys(input);
  if (!isValidSafeId(expectedTenant) || (expectedFleet !== undefined && !isValidSafeId(expectedFleet)) ||
      (mode === "resume" ? keys.length !== 0 : keys.length !== 1 ||
        !(mode === "register" ? ["bootstrap_credential", "registration_key"] : ["install_credential"]).includes(keys[0]) ||
        !secret(input[keys[0] as keyof Input]))) fail("input_invalid");
  await withLock(async () => {
    let j = readJournal();
    if (j && (j.gateway !== origin || j.expected_tenant !== expectedTenant ||
        (expectedFleet !== undefined && j.expected_fleet !== expectedFleet))) fail("scope_mismatch");
    if (j?.status === "held") fail("held");
    if (j && j.hostname !== hostname()) fail("scope_mismatch");
    if (!j) {
      if (mode === "resume") fail("state_invalid");
      const platform = process.platform;
      const arch = process.arch === "x64" ? "amd64" : process.arch;
      j = {schema_version: 1, status: "pending", mode, gateway: origin, api_prefix: "/api/v1",
        hostname: hostname(), platform, arch, created_at: new Date().toISOString(), expected_tenant: expectedTenant,
        ...(expectedFleet !== undefined ? {expected_fleet: expectedFleet} : {})};
      if (mode === "register") {
        j.install_uuid = randomUUID();
        j.machine_fingerprint = createHash("sha256").update(
          [j.install_uuid, j.hostname, platform, arch].join("\0")).digest("hex");
      }
      if (mode === "handoff") { j.install_credential = input.install_credential; j.status = "credential_saved"; }
      save(j);
    } else if (mode !== "resume" && (j.mode !== mode || j.status !== "pending")) fail("state_invalid");
    if (j.status === "pending") {
      if (mode !== "register") fail("input_invalid");
      const result = await requestInstallJson(j, "/installs/register", input.bootstrap_credential, {
        install_uuid: j.install_uuid, machine_fingerprint: j.machine_fingerprint,
        hostname: j.hostname, platform: j.platform, arch: j.arch,
        ...(input.registration_key ? {registration_key: input.registration_key} : {}),
      });
      if (!secret(result.install_credential) || !isValidSafeId(result.tenant_id) || !date(result.issued_at) ||
          (result.expires_at != null && !date(result.expires_at)) ||
          (result.fleet_id != null && !isValidSafeId(result.fleet_id))) fail("response_invalid");
      j.install_credential = result.install_credential; j.tenant_id = result.tenant_id;
      j.fleet_id = result.fleet_id as string | null | undefined;
      j.expires_at = result.expires_at as string | null | undefined;
      j.status = "credential_saved"; save(j);
    }
    if (expired(j)) hold(j, "credential_expired");
    if ((j.tenant_id && j.tenant_id !== expectedTenant) ||
        (j.mode === "register" && j.expected_fleet !== undefined && j.fleet_id !== j.expected_fleet)) hold(j, "scope_mismatch");
    // Withdraw the previous verification before a new explicit check; failures
    // must not leave an old verified snapshot available to future consumers.
    if (j.status === "verified") { j.status = "credential_saved"; save(j); }
    const context = await requestInstallJson(j, "/admin/agent-connect-context", j.install_credential);
    if (!uuid(context.install_uuid) || !isValidSafeId(context.org_id) || !isValidSafeId(context.tenant_id) || !text(context.hostname))
      hold(j, "response_invalid");
    if (context.tenant_id !== expectedTenant || (j.tenant_id && context.tenant_id !== j.tenant_id) ||
        (j.install_uuid && context.install_uuid !== j.install_uuid) || context.hostname !== j.hostname ||
        (j.org_id && context.org_id !== j.org_id)) hold(j, "scope_mismatch");
    j.install_uuid = context.install_uuid; j.org_id = context.org_id; j.tenant_id = context.tenant_id;
    if (j.fleet_id === undefined) hold(j, "fleet_unknown");
    j.status = "verified"; save(j);
  });
}
