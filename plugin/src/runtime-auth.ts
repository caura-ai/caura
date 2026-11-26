/** Explicit request authority; construction and import perform no I/O or environment reads. */
import {EnrollmentError, readVerifiedInstallAuthority, requestInstallJson} from "./install-authority.js";
import {InstallAgentError, invalidateInstallAgentKeys, resolveInstallAgentKey} from "./install-agent-auth.js";
import {isLoopbackHost, isValidSafeId} from "./validation.js";

export const RUNTIME_AUTH_SETTINGS = Object.freeze({mode: "CAURA_AUTH_MODE",
  manualKey: "CAURA_MANUAL_AGENT_API_KEY", manualAgentId: "CAURA_MANUAL_AGENT_ID",
  manualTenantId: "CAURA_MANUAL_TENANT_ID"});
export interface RuntimeAuthConfig {
  mode?: unknown; gateway: string; apiPrefix: string; tenantId?: string; fleetId?: string;
  standaloneKey?: string; manualKey?: string; manualAgentId?: string; manualTenantId?: string;
  allowInsecureHttp?: boolean;
}
export interface AuthorityRequest {
  authority: "none" | "agent" | "install"; method: string; path: string; agentId?: string;
  extraHeaders?: Record<string, string>;
  signal?: AbortSignal;
}
export interface AuthorizedRequest {headers: Record<string, string>; tenantId?: string; fleetId?: string | null;}
export class RuntimeAuthError extends Error {}
function fail(code: string): never {throw new RuntimeAuthError(`runtime_auth_${code}`);}
function normalized(e: unknown): RuntimeAuthError {
  if (e instanceof RuntimeAuthError) return e;
  if ((e instanceof EnrollmentError || e instanceof InstallAgentError) &&
      /^(?:enrollment|install_agent)_[a-z0-9_]+$/.test(e.message))
    return new RuntimeAuthError(e.message.replace(/^(?:enrollment|install_agent)_/, "runtime_auth_"));
  return new RuntimeAuthError("runtime_auth_request_failed");
}
const secret = (v: unknown): v is string => typeof v === "string" && /^[\x21-\x7e]{1,4096}$/.test(v);
const PUBLIC = new Set(["/plugin-source", "/plugin-manifest", "/tool-descriptions"]);
// These node surfaces authorize ownership against the authenticated principal.
// core-api: fleet.py heartbeat/command_result, documents.py installable_skills,
// skill_delivery.py record_installations. whoami probes authenticated identity.
function nodeRoute(method: string, path: string): boolean {
  return (method === "GET" && path === "/whoami") || (method === "POST" &&
    (["/fleet/heartbeat", "/skills/installable", "/skills/installations"].includes(path) ||
      /^\/fleet\/commands\/[0-9a-f-]{36}\/result$/.test(path)));
}
function checkedRequest(r: AuthorityRequest): Headers {
  if (!r || !["GET", "POST", "PUT", "PATCH", "DELETE"].includes(r.method) ||
      typeof r.path !== "string" || !/^\/(?:[\w.~\-]|%[0-9a-f]{2}|\/)+$/i.test(r.path)) fail("request_invalid");
  let path = r.path;
  for (let depth = 0;; depth++) {
    if (path.slice(1).split("/").some(s => !s || s === "." || s === "..") || /[\\\x00-\x1f\x7f]/.test(path)) fail("request_invalid");
    if (!/%[0-9a-f]{2}/i.test(path)) break;
    if (depth >= 4) fail("request_invalid");
    try {path = decodeURIComponent(path);} catch {return fail("request_invalid");}
  }
  if (PUBLIC.has(path)) {
    if (r.method !== "GET" || r.authority !== "none") fail("authority_mismatch");
  } else if (nodeRoute(r.method, path)) {
    if (r.authority !== "install") fail("authority_mismatch");
  } else if (r.authority !== "agent" || /^\/(?:admin|fleet|skills)(?:\/|$)/.test(path)) fail("authority_mismatch");
  if (r.authority === "agent" && !isValidSafeId(r.agentId)) fail("agent_required");
  const headers = new Headers(r.extraHeaders);
  for (const key of headers.keys()) {
    if (key !== "x-caura-session-id" && /^(?:authorization|proxy-authorization|cookie|x-(?:api-key|tenant-|agent-|install-|caura-|gateway-|org-|user-|auth-|capabilities|readable-))/i.test(key))
      fail("header_forbidden");
  }
  return headers;
}

/** Construction only captures supplied configuration. initialize() is an explicit action. */
export function createRuntimeAuth(config: RuntimeAuthConfig) {
  const c = Object.freeze({...config});
  let initialized: Promise<void> | undefined, manualHeld = false;
  let retryAt = 0, startError: RuntimeAuthError | undefined;
  function mode(): "managed" | "standalone" | "manual" {
    if (c.mode === undefined || (typeof c.mode === "string" && !c.mode.trim())) fail("mode_required");
    if (c.mode !== "managed" && c.mode !== "standalone" && c.mode !== "manual") fail("mode_invalid");
    return c.mode;
  }
  function connection(selected: string): void {
    let u: URL;
    try {u = new URL(c.gateway);} catch {return fail("gateway_invalid");}
    if (u.origin !== c.gateway || u.username || u.password || u.pathname !== "/" || u.search || u.hash ||
        !(u.protocol === "https:" || (u.protocol === "http:" && (isLoopbackHost(u.hostname) ||
          (selected === "standalone" && (c.allowInsecureHttp === true || !c.standaloneKey))))) ||
        typeof c.apiPrefix !== "string" || !/^\/(?:[\w-]+\/)*[\w-]+$/.test(c.apiPrefix)) fail("gateway_invalid");
  }
  function localInstall() {
    const j = readVerifiedInstallAuthority();
    if (!j?.org_id || !j.tenant_id || !j.install_uuid || !j.install_credential) fail("authority_unavailable");
    if (c.gateway !== j.gateway || c.apiPrefix !== j.api_prefix ||
        (c.tenantId !== undefined && c.tenantId !== "" && c.tenantId !== j.tenant_id) ||
        (c.fleetId !== undefined && c.fleetId !== "" && c.fleetId !== j.fleet_id)) fail("scope_mismatch");
    return j;
  }
  function unchanged(j: ReturnType<typeof localInstall>): void {
    const current = localInstall();
    for (const k of ["gateway", "api_prefix", "org_id", "tenant_id", "install_uuid", "hostname", "fleet_id", "expires_at", "install_credential"] as const)
      if (current[k] !== j[k]) fail("authority_changed");
  }
  async function freshInstall() {
    const j = localInstall(), v = await requestInstallJson(j, "/admin/agent-connect-context", j.install_credential);
    if (v.org_id !== j.org_id || v.tenant_id !== j.tenant_id || v.install_uuid !== j.install_uuid ||
        v.hostname !== j.hostname || (v.fleet_id !== undefined && v.fleet_id !== j.fleet_id)) fail("scope_mismatch");
    unchanged(j); return j;
  }
  async function start(): Promise<void> {
    const selected = mode(); connection(selected);
    if (selected === "managed") {await freshInstall(); return;}
    if (selected === "standalone") {
      if (c.standaloneKey !== undefined && c.standaloneKey !== "" && !secret(c.standaloneKey)) fail("key_invalid");
      return;
    }
    if (!secret(c.manualKey) || !isValidSafeId(c.manualAgentId) || !isValidSafeId(c.manualTenantId)) fail("manual_config_invalid");
    // Gateway emits row kind unchanged: auth.py:7285/7409 (agent_key).
    // Core health.py:188-205 reports it only in the verified gateway branch.
    const api_prefix = c.apiPrefix;
    if (api_prefix !== "/api/v1") fail("scope_mismatch");
    const v = await requestInstallJson({gateway: c.gateway, api_prefix}, "/whoami", c.manualKey);
    if (v.auth_source !== "gateway-header" || v.via_gateway !== true || v.key_kind !== "agent_key" ||
        v.tenant_id !== c.manualTenantId || v.agent_id !== c.manualAgentId) fail("manual_proof_invalid");
  }
  function initialize(): Promise<void> {
    if (manualHeld) return Promise.reject(new RuntimeAuthError("runtime_auth_manual_held"));
    if (!initialized && startError && Date.now() < retryAt) return Promise.reject(startError);
    initialized ??= start().catch(e => {
      initialized = undefined; startError = normalized(e); retryAt = Date.now() + 30_000;
      if (c.mode === "manual" && /^runtime_auth_http_(401|403)$/.test(startError.message)) manualHeld = true;
      throw startError;
    });
    return initialized.then(() => {if (manualHeld) fail("manual_held");});
  }
  async function authorize(request: AuthorityRequest): Promise<AuthorizedRequest> {
    try {
      if (!initialized) fail("not_initialized");
      await initialized;
      if (manualHeld) fail("manual_held");
      const selected = mode(), headers = checkedRequest(request);
      let key: string | undefined, tenantId = c.tenantId, fleetId: string | null | undefined = c.fleetId;
      if (request.authority !== "none") {
        if (selected === "standalone") key = c.standaloneKey;
        else if (selected === "manual") {
          if (request.authority !== "agent" || request.agentId !== c.manualAgentId) fail("authority_mismatch");
          key = c.manualKey; tenantId = c.manualTenantId;
        } else {
          const j = request.authority === "install" ? await freshInstall() : localInstall();
          key = request.authority === "install" ? j.install_credential : await resolveInstallAgentKey({
            gateway: j.gateway, apiPrefix: j.api_prefix, orgId: j.org_id!, tenantId: j.tenant_id!,
            installUuid: j.install_uuid!, agentId: request.agentId!, capabilities: ["read", "write"], signal: request.signal});
          unchanged(j); tenantId = j.tenant_id; fleetId = j.fleet_id;
        }
      }
      if (manualHeld) fail("manual_held"); // A concurrent 401/403 cannot be undone by a late probe.
      if (key) headers.set("X-API-Key", key);
      return {headers: Object.fromEntries(headers), tenantId, fleetId};
    } catch (e) {throw normalized(e);}
  }
  function observeResponse(status: number, key?: string): void {
    if (c.mode === "managed" && key && (status === 401 || status === 403)) invalidateInstallAgentKeys();
    if (c.mode === "manual" && (status === 401 || status === 403)) manualHeld = true;
  }
  return {initialize, authorize, observeResponse};
}
