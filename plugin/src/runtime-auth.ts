/** Preparatory request authority. No runtime imports, environment reads or startup I/O. */
import {EnrollmentError, readVerifiedInstallAuthority, requestInstallJson} from "./install-authority.js";
import {InstallAgentError, resolveInstallAgentKey} from "./install-agent-auth.js";
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
      typeof r.path !== "string" || !/^\/(?:[\w-]+\/)*[\w-]+$/.test(r.path)) fail("request_invalid");
  if (PUBLIC.has(r.path)) {
    if (r.method !== "GET" || r.authority !== "none") fail("authority_mismatch");
  } else if (nodeRoute(r.method, r.path)) {
    if (r.authority !== "install") fail("authority_mismatch");
  } else if (r.authority !== "agent" || r.path.startsWith("/admin/") ||
      r.path.startsWith("/fleet/") || r.path.startsWith("/skills/")) fail("authority_mismatch");
  if (r.authority === "agent" && !isValidSafeId(r.agentId)) fail("agent_required");
  const headers = new Headers(r.extraHeaders);
  for (const key of headers.keys()) {
    if (/^(?:authorization|proxy-authorization|cookie|x-(?:api-key|tenant-|agent-|install-|caura-|gateway-|org-|user-|auth-|capabilities|readable-))/i.test(key))
      fail("header_forbidden");
  }
  return headers;
}

/** Construction only captures supplied configuration. initialize() is an explicit action. */
export function createRuntimeAuth(config: RuntimeAuthConfig) {
  const c = Object.freeze({...config});
  let initialized: Promise<void> | undefined, manualHeld = false;
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
          (selected === "standalone" && c.allowInsecureHttp === true)))) ||
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
    // A failed initialization is not an automatic recovery or a new identity.
    initialized ??= start().catch(e => {if (c.mode === "manual") manualHeld = true; throw normalized(e);});
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
            installUuid: j.install_uuid!, agentId: request.agentId!, capabilities: ["read", "write"]});
          unchanged(j); tenantId = j.tenant_id; fleetId = j.fleet_id;
        }
      }
      if (manualHeld) fail("manual_held"); // A concurrent 401/403 cannot be undone by a late probe.
      if (key) headers.set("X-API-Key", key);
      return {headers: Object.fromEntries(headers), tenantId, fleetId};
    } catch (e) {throw normalized(e);}
  }
  function observeResponse(status: number): void {
    if (c.mode === "manual" && (status === 401 || status === 403)) manualHeld = true;
  }
  return {initialize, authorize, observeResponse};
}
