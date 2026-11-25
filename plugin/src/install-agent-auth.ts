/** Installation-bound agent delegation; no import-time I/O. */
import {randomUUID} from "node:crypto";
import {EnrollmentError, readVerifiedInstallAuthority, requestInstallJson} from "./install-authority.js";
import {getInstallAgentKeysPath} from "./paths.js";
import {privateInstallState} from "./private-install-state.js";
import {isValidSafeId} from "./validation.js";

export interface InstallAgentOptions {
  gateway: string; apiPrefix: string; orgId: string; tenantId: string;
  installUuid: string; agentId: string; capabilities: string[]; renew?: boolean;
}
type Binding = {gateway: string; api_prefix: string; org_id: string; tenant_id: string;
  install_uuid: string; hostname: string};
type Credential = Record<string, unknown> & {id: string; expires_at: string; capabilities: string[]};
type Operation = {id: string; kind: "create" | "rotate"; target_id?: string};
type Entry = {status: "pending" | "held" | "ready"; operation: Operation; key?: string; credential?: Credential;
  previous?: {key: string; credential: Credential}};
type Cache = {schema_version: 1; binding: Binding; entries: Record<string, Entry>};
export class InstallAgentError extends Error {}
function fail(code: string): never {throw new InstallAgentError(`install_agent_${code}`);}
const state = privateInstallState(getInstallAgentKeysPath, fail, e => e instanceof InstallAgentError);
const flights = new Map<string, Promise<string>>();
const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const secret = (v: unknown): v is string => typeof v === "string" && v.length > 0 &&
  v.length <= 4096 && /^[\x21-\x7e]+$/.test(v);
function object(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) fail("state_invalid");
  return v as Record<string, unknown>;
}
function timestamp(v: unknown): number {
  if (typeof v !== "string" || !/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(v) ||
      !Number.isFinite(Date.parse(v))) fail("credential_invalid");
  return Date.parse(v);
}
function capabilities(v: unknown): string[] {
  if (!Array.isArray(v) || !v.length || v.length > 2 ||
      v.some(c => c !== "read" && c !== "write") || new Set(v).size !== v.length) fail("scope_mismatch");
  return [...v].sort();
}
function checkedCredential(value: unknown, binding: Binding, agent: string, caps: string[],
  parentExpiry: string | null | undefined, allowExpired: boolean): Credential {
  const v = object(value), expiry = timestamp(v.expires_at);
  if (!uuid(v.id) || v.kind !== "agent_key" || v.org_id !== binding.org_id ||
      v.home_tenant_id !== binding.tenant_id || v.home_agent_id !== agent ||
      v.issuing_install_uuid !== binding.install_uuid || v.revoked_at != null ||
      !Array.isArray(v.source_tenant_ids) || v.source_tenant_ids.length !== 0 ||
      v.read_all_org_tenants !== false || v.bootstrap_allowed !== false || v.fleet_id != null ||
      JSON.stringify(capabilities(v.capabilities)) !== JSON.stringify(caps) ||
      (!allowExpired && expiry <= Date.now()) || expiry > Date.now() + 365 * 86400_000 ||
      (parentExpiry && expiry > timestamp(parentExpiry))) fail("credential_invalid");
  // Preserve bounded metadata, never a second copy of the raw secret or arbitrary server fields.
  return Object.fromEntries(["id", "org_id", "kind", "home_tenant_id", "home_agent_id",
    "issuing_install_uuid", "source_tenant_ids", "read_all_org_tenants", "capabilities",
    "expires_at", "revoked_at", "bootstrap_allowed", "fleet_id", "label", "rotated_from_id"]
    .filter(k => Object.hasOwn(v, k)).map(k => [k, v[k]])) as Credential;
}
function readCache(binding: Binding, parentExpiry: string | null | undefined): Cache {
  const raw = state.read();
  if (raw === undefined) return {schema_version: 1, binding, entries: {}};
  const v = object(raw), saved = object(v.binding);
  if (v.schema_version !== 1 || Object.keys(v).length !== 3 ||
      Object.keys(saved).length !== Object.keys(binding).length ||
      Object.entries(binding).some(([k, x]) => !Object.hasOwn(saved, k) || saved[k] !== x)) fail("state_invalid");
  const entries = object(v.entries);
  for (const [agent, value] of Object.entries(entries)) {
    const entry = object(value), op = object(entry.operation);
    if (!isValidSafeId(agent) || !["pending", "held", "ready"].includes(String(entry.status)) ||
        !uuid(op.id) || !["create", "rotate"].includes(String(op.kind)) ||
        (op.target_id !== undefined && !uuid(op.target_id)) ||
        (op.kind === "rotate" && !uuid(op.target_id))) fail("state_invalid");
    if (entry.status === "ready") {
      if (!secret(entry.key) || entry.previous !== undefined) fail("state_invalid");
      const c = object(entry.credential);
      checkedCredential(c, binding, agent, capabilities(c.capabilities), parentExpiry, true);
    } else if (entry.key !== undefined || entry.credential !== undefined) fail("state_invalid");
    if (entry.previous !== undefined) {
      const previous = object(entry.previous), c = object(previous.credential);
      if (Object.keys(previous).length !== 2 || !secret(previous.key) || c.id !== op.target_id)
        fail("state_invalid");
      checkedCredential(c, binding, agent, capabilities(c.capabilities), parentExpiry, true);
    }
  }
  return v as unknown as Cache;
}
function save(cache: Cache, replaced?: () => void): void {
  if (Buffer.byteLength(JSON.stringify(cache) + "\n") > 65536) fail("state_limit");
  state.save(cache, replaced);
}
function authorityFor(o: InstallAgentOptions) {
  const j = readVerifiedInstallAuthority();
  if (!j || !j.org_id || !j.tenant_id || !j.install_uuid || !j.install_credential) fail("authority_unavailable");
  if (o.gateway !== j.gateway || o.apiPrefix !== j.api_prefix || o.orgId !== j.org_id ||
      o.tenantId !== j.tenant_id || o.installUuid !== j.install_uuid) fail("scope_mismatch");
  return j;
}
function unchangedAuthority(o: InstallAgentOptions, previous: ReturnType<typeof authorityFor>): void {
  const current = authorityFor(o);
  if (current.install_credential !== previous.install_credential || current.expires_at !== previous.expires_at ||
      current.hostname !== previous.hostname || current.fleet_id !== previous.fleet_id) fail("authority_changed");
}
async function execute(o: InstallAgentOptions, rotate: boolean): Promise<string> {
  if (!isValidSafeId(o.agentId) || (o.renew !== undefined && typeof o.renew !== "boolean") ||
      (rotate && o.renew)) fail("input_invalid");
  const caps = capabilities(o.capabilities), j = authorityFor(o);
  const binding: Binding = {gateway: j.gateway, api_prefix: j.api_prefix, org_id: j.org_id!,
    tenant_id: j.tenant_id!, install_uuid: j.install_uuid!, hostname: j.hostname};
  let uncertain = false;
  return state.withLock(async () => {
    unchangedAuthority(o, j);
    const cache = readCache(binding, j.expires_at);
    const entry = Object.hasOwn(cache.entries, o.agentId) ? cache.entries[o.agentId] : undefined;
    if (entry && entry.status !== "ready") fail("held");
    const request = (path: string, body?: object) => requestInstallJson(j, path, j.install_credential, body);
    const context = await request("/admin/agent-connect-context");
    if (context.org_id !== binding.org_id || context.tenant_id !== binding.tenant_id ||
        context.install_uuid !== binding.install_uuid || context.hostname !== binding.hostname) fail("scope_mismatch");
    unchangedAuthority(o, j);
    const base = `/admin/orgs/${encodeURIComponent(o.orgId)}/api-credentials`;
    let previous: Credential | undefined;
    if (entry) {
      previous = checkedCredential(entry.credential, binding, o.agentId, caps, j.expires_at, !!o.renew);
      const live = checkedCredential(await request(`${base}/${encodeURIComponent(previous.id)}`),
        binding, o.agentId, caps, j.expires_at, !!o.renew);
      if (live.id !== previous.id || live.expires_at !== previous.expires_at) fail("credential_changed");
      if (!rotate && !o.renew) {unchangedAuthority(o, j); return entry.key!;}
      if (o.renew && timestamp(live.expires_at) > Date.now()) fail("renewal_not_due");
    } else {
      if (rotate || o.renew) fail("credential_missing");
      const query = new URLSearchParams({kind: "agent_key", home_tenant_id: o.tenantId,
        home_agent_id: o.agentId, include_revoked: "true"});
      const catalog = await request(`${base}?${query}`);
      if (!Array.isArray(catalog.items)) fail("response_invalid");
      // Metadata cannot recover a lost raw key. Never silently adopt or replace it.
      if (catalog.items.length) fail("catalog_held");
    }
    unchangedAuthority(o, j);
    const operation: Operation = {id: randomUUID(), kind: rotate ? "rotate" : "create",
      ...(previous ? {target_id: previous.id} : {})};
    cache.entries = {...cache.entries, [o.agentId]: {status: "pending", operation,
      ...(previous ? {previous: {key: entry!.key!, credential: previous}} : {})}};
    // Before rename, no state or server mutation occurred. Afterwards, preserve the hold on error.
    save(cache, () => {uncertain = true;}); // Durable intent precedes the server side effect.
    const label = `Plugin agent credential ${operation.id}`;
    const result = rotate ? await request(`${base}/${encodeURIComponent(previous!.id)}/rotate`, {label}) :
      await request(base, {kind: "agent_key", home_tenant_id: o.tenantId, home_agent_id: o.agentId,
        source_tenant_ids: [], read_all_org_tenants: false, capabilities: caps, label, bootstrap_allowed: false});
    const credential = checkedCredential(result, binding, o.agentId, caps, j.expires_at, false);
    if (!secret(result.raw_key) || (previous && credential.id === previous.id) ||
        (rotate && credential.rotated_from_id !== previous!.id) ||
        (rotate && timestamp(credential.expires_at) > timestamp(previous!.expires_at))) fail("response_invalid");
    // Preserve the validated acknowledgement before consulting mutable enrollment state.
    cache.entries = {...cache.entries, [o.agentId]: {status: "ready", operation, key: result.raw_key, credential}};
    save(cache); // No memory-only continuation. An uncertain final save retains the lock.
    uncertain = false; // A durable acknowledgement no longer needs an operation hold.
    unchangedAuthority(o, j);
    return result.raw_key;
  }, () => uncertain);
}
function run(options: InstallAgentOptions, rotate: boolean): Promise<string> {
  let o: InstallAgentOptions, key: string;
  try {
    o = {...options, capabilities: [...options.capabilities]};
    key = JSON.stringify([rotate, o.gateway, o.apiPrefix, o.orgId, o.tenantId, o.installUuid,
      o.agentId, capabilities(o.capabilities), !!o.renew]);
  } catch {return Promise.reject(new InstallAgentError("install_agent_input_invalid"));}
  const pending = flights.get(key);
  if (pending) return pending;
  const action = execute(o, rotate).catch(e => {
    if (e instanceof InstallAgentError) throw e;
    if (e instanceof EnrollmentError && /^enrollment_[a-z0-9_]+$/.test(e.message))
      return fail(e.message.slice("enrollment_".length));
    return fail("state_unavailable");
  }).finally(() => {flights.delete(key);});
  flights.set(key, action);
  return action;
}
/** Resolution never renews an expired key unless the caller explicitly requests renew. */
export function resolveInstallAgentKey(options: InstallAgentOptions): Promise<string> {return run(options, false);}
/** Rotate only the stored, live, installation-owned credential; never extend expiry. */
export function rotateInstallAgentKey(options: InstallAgentOptions): Promise<string> {return run(options, true);}
